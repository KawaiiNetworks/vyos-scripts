import requests
import os
import subprocess
import ipaddress
import json
import yaml
import time
import dns.resolver
from copy import deepcopy
from aggregate_prefixes import aggregate_prefixes


work_dir = os.path.dirname(os.path.abspath(__file__))
config = yaml.safe_load(
    open(os.path.join(work_dir, "config.yaml"), "r", encoding="utf-8")
)

local_asn = config["local-asn"]
router_list = config["router"]

defaultconfig = ""
for root, dirs, files in os.walk(os.path.join(work_dir, "defaults")):
    for file in files:
        defaultconfig += open(os.path.join(root, file), "r", encoding="utf-8").read()
        defaultconfig += "\n"
defaultconfig = defaultconfig.replace("%ASN%", str(local_asn))

asset_name_map = {}
"""as:as-set"""

cone_map = {}
"""as:[as-set member]"""

prefix_matrix_map = {}
"""(ipversion, as):[(network, length, ge, le, object_mask, invert_mask)]"""


def get_router_id(router_name):
    """use dns to get router-id"""
    try:
        answer = dns.resolver.resolve(router_name, "A")
        if len(answer) != 1:
            print("router-id for {router_name} is not unique")
            return None
        return answer[0].address
    except Exception:
        return None


def get_asset_name(asn):
    """use peeringdb to get as-set name"""

    time.sleep(5)

    if asn in asset_name_map:
        return deepcopy(asset_name_map[asn])

    url = f"https://www.peeringdb.com/api/net?asn={asn}"

    try:
        response = requests.get(url, timeout=10)
        res = response.json()["data"][0]["irr_as_set"].split()
        new_res = []
        for n in res:
            if "::" in n:
                # 虽然这里有一个-S且别的命令那也有-S，但是这边-S在后面，会覆盖掉那些更多的数据库，使得最终只用指定的数据库
                new_res.append(f"{n.split('::')[1]} -S {n.split('::')[0]}")
            else:
                new_res.append(n)
    except Exception as e:
        print(f"get as-set name for AS{asn} failed: {e}")
        new_res = [f"AS{asn}"]

    asset_name_map[asn] = new_res
    print(f"AS{asn} as-set name: {new_res}")
    return deepcopy(new_res)


def get_as_set_member(asn):
    """use bgpq4 to get as-set member"""

    if asn in cone_map:
        return deepcopy(cone_map[asn])

    asset_name_list = get_asset_name(asn)  # an asn may have multiple as-set
    if not asset_name_list:
        cone_map[asn] = [asn]
        print(f"AS{asn} cone list: {cone_map[asn]}")
        return cone_map[asn]

    res = []

    for asset_name in asset_name_list:
        cmd = (
            f"bgpq4 -S RPKI,AFRINIC,ARIN,APNIC,LACNIC,RIPE,RADB,ALTDB -jt {asset_name}"
        )
        result = subprocess.run(
            cmd,
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        res += json.loads(result.stdout)["NN"]
    res = sorted(list(set(res)))
    cone_map[asn] = res
    print(f"AS{asn} cone list: {res}")
    return deepcopy(res)


def get_prefix_matrix(ipversion, asn, aggregate=False):
    """use bgpq4 to get prefix matrix"""

    if (ipversion, asn) in prefix_matrix_map:
        return deepcopy(prefix_matrix_map[(ipversion, asn)])

    cmd = rf'bgpq4 -S RPKI,AFRINIC,ARIN,APNIC,LACNIC,RIPE,RADB,ALTDB -{ipversion} -A -F "%n,%l,%a,%A\n" as{asn} -l AS{asn}'
    prefix_matrix = []
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        res = result.stdout.splitlines()
        res = [x for x in res if x]
        for line in res:
            prefix_matrix.append(tuple(line.split(",")))

        if aggregate:
            prefix_matrix = list(
                aggregate_prefixes([f"{p[0]}/{p[1]}" for p in prefix_matrix])
            )
            prefix_matrix = [
                (
                    x.network_address.exploded,
                    x.prefixlen,
                    x.prefixlen,
                    24 if ipversion == 4 else 48,
                )
                for x in prefix_matrix
            ]

    except Exception as e:
        raise e
    prefix_matrix_map[(ipversion, asn)] = prefix_matrix
    print(f"AS{asn} prefix{ipversion} matrix generated.")
    return deepcopy(prefix_matrix)


def get_vyos_as_path(asn):
    """use bgpq4 to get as-path filter"""

    full_vyos_cmd = f"""
    delete policy as-path-list AS{asn}-IN
    set policy as-path-list AS{asn}-IN rule 10 action permit
    set policy as-path-list AS{asn}-IN rule 10 regex '^{asn}(_{asn})*$'
    """

    cone_list = get_as_set_member(asn)
    if asn in cone_list:
        cone_list.remove(asn)
    cone_list = [str(x) for x in cone_list]

    if len(cone_list) + 1 > config["as-set"]["member-limit"]:
        if config["as-set"]["limit-violation"] == "accept":
            full_vyos_cmd += f"""
            set policy as-path-list AS{asn}-IN rule 20 action permit
            set policy as-path-list AS{asn}-IN rule 20 regex '^{asn}(_[0-9]+)*$'
            """
            print(
                f"Warn: AS{asn} as-path filter generated. But cone number is {len(cone_list)+1}, filter will accept all as-path."
            )
            return full_vyos_cmd
        elif config["as-set"]["limit-violation"] == "deny":
            full_vyos_cmd += f"""
            set policy as-path-list AS{asn}-IN rule 20 action deny
            set policy as-path-list AS{asn}-IN rule 20 regex '.*'
            """
            print(
                f"Warn: AS{asn} as-path filter generated. But cone number is {len(cone_list)+1}, filter will deny all as-path except {asn}."
            )
            return full_vyos_cmd
        else:
            raise ValueError("as-set limit-violation must be accept, deny")

    if len(cone_list) > 0:
        full_vyos_cmd += f"""
        set policy as-path-list AS{asn}-IN rule 20 action permit
        set policy as-path-list AS{asn}-IN rule 20 regex '^{asn}(_[0-9]+)*_({"|".join(cone_list)})$'
        """
    print(f"AS{asn} as-path filter generated.")
    return full_vyos_cmd


def get_vyos_prefix_list(
    ipversion, asn, max_length=None, filter_name=None, cone=False, aggregate=False
):
    """use bgpq4 to get prefix list filter"""

    # if aggregate is True it can reduce the number of configuration lines

    def vyos_cmd(network, length, ge, le, rule):
        # r = str(rule)
        return f"""
        set policy {pl} {fn} rule {rule} action permit
        set policy {pl} {fn} rule {rule} prefix {network}/{length}
        set policy {pl} {fn} rule {rule} ge {ge}
        set policy {pl} {fn} rule {rule} le {max_length if max_length else le}
        """

    def vyos_cmd_when_limit_violation(pl, fn, cone_count, prefix_count):
        if config["as-set"]["limit-violation"] == "accept":
            print(
                f"Warn: AS{asn} cone prefix{ipversion} list generated. But cone number({cone_count})/prefix number({prefix_count}) is too large, filter will accept all prefix."
            )
            return f"""
            set policy {pl} {fn} rule 10 action permit
            set policy {pl} {fn} rule 10 prefix {"0.0.0.0/0" if ipversion == 4 else "::/0"}
            set policy {pl} {fn} rule 10 le {24 if ipversion == 4 else 48}
            """
        elif config["as-set"]["limit-violation"] == "deny":
            print(
                f"Warn: AS{asn} cone prefix{ipversion} list generated. But cone number({cone_count})/prefix number({prefix_count}) is too large, filter will deny all prefix except {asn}."
            )
            return f"""
            set policy {pl} {fn} rule 10 action deny
            set policy {pl} {fn} rule 10 prefix {"0.0.0.0/0" if ipversion == 4 else "::/0"}
            set policy {pl} {fn} rule 10 le {32 if ipversion == 4 else 128}
            """
        else:
            raise ValueError("as-set limit-violation must be accept, deny")

    if cone:
        fn = filter_name if filter_name else f"AS{asn}-CONE"
    else:
        fn = filter_name if filter_name else f"AS{asn}"

    if ipversion == 4:
        pl = "prefix-list"
    elif ipversion == 6:
        pl = "prefix-list6"
    else:
        raise ValueError("ipversion must be 4 or 6")

    full_vyos_cmd = f"delete policy {pl} {fn}\n"

    if cone:
        cone_asn_list = get_as_set_member(asn)

        if len(cone_asn_list) > config["as-set"]["member-limit"]:
            full_vyos_cmd += vyos_cmd_when_limit_violation(
                pl, fn, len(cone_asn_list), "not counted"
            )
            return full_vyos_cmd

        prefix_matrix = []
        for x in cone_asn_list:
            prefix_matrix.extend(get_prefix_matrix(ipversion, x, aggregate=aggregate))
        if len(prefix_matrix) > config["as-set"]["prefix-limit"]:
            full_vyos_cmd += vyos_cmd_when_limit_violation(
                pl, fn, len(cone_asn_list), len(prefix_matrix)
            )
            return full_vyos_cmd
    else:
        prefix_matrix = get_prefix_matrix(ipversion, asn, aggregate=aggregate)

    prefix_matrix = sorted(list(set(prefix_matrix)))
    if len(prefix_matrix) > 0:
        c = 1
        for prefix in prefix_matrix:
            full_vyos_cmd += vyos_cmd(*prefix, c)
            c += 1
        del c
        print(f"AS{asn} {'cone ' if cone else ''}prefix{ipversion} list generated.")
    else:
        full_vyos_cmd += f"""
        set policy {pl} {fn} rule 10 action deny
        set policy {pl} {fn} rule 10 prefix {"0.0.0.0/0" if ipversion == 4 else "::/0"}
        set policy {pl} {fn} rule 10 le {32 if ipversion == 4 else 128}
        """
        print(
            f"AS{asn} {'cone ' if cone else ''}prefix{ipversion} list generated. But no prefix in list."
        )
    return full_vyos_cmd


def get_vyos_as_filter(asn, _role):
    full_vyos_cmd = ""
    full_vyos_cmd += get_vyos_as_path(asn)
    full_vyos_cmd += get_vyos_prefix_list(4, asn, cone=True)
    full_vyos_cmd += get_vyos_prefix_list(6, asn, cone=True)
    full_vyos_cmd += f"""
    delete policy route-map FILTER-AS{asn}-IN
    set policy route-map FILTER-AS{asn}-IN rule 10 action permit
    set policy route-map FILTER-AS{asn}-IN rule 10 match as-path AS{asn}-IN
    set policy route-map FILTER-AS{asn}-IN rule 10 on-match next
    set policy route-map FILTER-AS{asn}-IN rule 20 action permit
    set policy route-map FILTER-AS{asn}-IN rule 20 match ip address prefix-list AS{asn}-CONE
    set policy route-map FILTER-AS{asn}-IN rule 30 action permit
    set policy route-map FILTER-AS{asn}-IN rule 30 match ipv6 address prefix-list AS{asn}-CONE
    """

    return full_vyos_cmd


def get_vyos_protocol_rpki(server_list):
    """cmd to configure vyos protocol rpki"""

    cmd = """
    delete protocols rpki
    """
    for server in server_list:
        cmd += f"""set protocols rpki cache {server["server"]} port {server["port"]}
        set protocols rpki cache {server["server"]} preference {server["preference"]}
        """
    return cmd


def vyos_neighbor_in_optional_attributes(neighbor, route_map_in_name):
    """cmd to configure vyos neighbor optional attributes"""

    f = ""
    if "local-pref" in neighbor:
        f += f"""
        set policy route-map {route_map_in_name} rule 200 set local-preference '{neighbor["local-pref"]}'
        """
    if "metric" in neighbor:
        f += f"""
        set policy route-map {route_map_in_name} rule 200 set metric {neighbor["metric"]}
        """

    if "post-import-accept" in neighbor:
        r = 201
        for c in neighbor["post-import-accept"]:
            f += f"""
            set policy route-map {route_map_in_name} rule {r} action {c["action"]}
            set policy route-map {route_map_in_name} rule {r} match {c["match"]}
            """
            if "set" in c:
                f += f"""
                set policy route-map {route_map_in_name} rule {r} set {c["set"]}
                """
            # 或者给这些on-match-next false的全部跳转到1000？目前没跳转，不影响
            if c["action"] == "permit" and ("on-match-next" not in c or c["on-match-next"]):
                f += f"""
                set policy route-map {route_map_in_name} rule {r} on-match next
                """
            r += 1
    return f


def vyos_neighbor_out_optional_attributes(neighbor, route_map_out_name):
    """cmd to configure vyos neighbor optional attributes"""

    f = ""
    if "prepend" in neighbor:
        f += f"""
        set policy route-map {route_map_out_name} rule 200 set as-path prepend '{neighbor["prepend"]}'
        """

    if "post-export-accept" in neighbor:
        r = 201
        for c in neighbor["post-export-accept"]:
            f += f"""
            set policy route-map {route_map_out_name} rule {r} action {c["action"]}
            set policy route-map {route_map_out_name} rule {r} match {c["match"]}
            """
            if "set" in c:
                f += f"""
                set policy route-map {route_map_out_name} rule {r} set {c["set"]}
                """
            # 或者给这些on-match-next false的全部跳转到1000？目前没跳转，不影响
            if c["action"] == "permit" and ("on-match-next" not in c or c["on-match-next"]):
                f += f"""
                set policy route-map {route_map_out_name} rule {r} on-match next
                """
            r += 1

    return f


def get_vyos_protocol_bgp_ibgp(neighbor, neighbor_id):
    """cmd to configure vyos protocol bgp ibgp"""

    asn = local_asn
    neighbor_address = neighbor["neighbor-address"]
    route_map_in_name = f"IBGP-IN-{neighbor_id}"
    route_map_out_name = f"IBGP-OUT-{neighbor_id}"

    final_filter = f"""
    delete policy route-map {route_map_in_name}
    set policy route-map {route_map_in_name} rule 10 action permit
    set policy route-map {route_map_in_name} rule 10 call IBGP-IN
    set policy route-map {route_map_in_name} rule 10 on-match next
    set policy route-map {route_map_in_name} rule 200 action permit
    set policy route-map {route_map_in_name} rule 200 on-match next
    set policy route-map {route_map_in_name} rule 1000 action permit
    
    delete policy route-map {route_map_out_name}
    set policy route-map {route_map_out_name} rule 10 action permit
    set policy route-map {route_map_out_name} rule 10 call IBGP-OUT
    set policy route-map {route_map_out_name} rule 10 on-match next
    set policy route-map {route_map_out_name} rule 200 action permit
    set policy route-map {route_map_out_name} rule 200 on-match next
    set policy route-map {route_map_out_name} rule 1000 action permit
    """

    final_filter += vyos_neighbor_in_optional_attributes(neighbor, route_map_in_name)
    final_filter += vyos_neighbor_out_optional_attributes(neighbor, route_map_out_name)

    ipversion = ipaddress.ip_address(neighbor_address).version

    password = neighbor["password"] if "password" in neighbor else None

    bgp_cmd = f"""
    delete protocols bgp neighbor {neighbor_address}
    set protocols bgp neighbor {neighbor_address} description '{neighbor["description"] if "description" in neighbor else f"AS{asn}-ibgp"}'
    set protocols bgp neighbor {neighbor_address} graceful-restart enable
    set protocols bgp neighbor {neighbor_address} remote-as {asn}
    {f"set protocols bgp neighbor {neighbor_address} password '{password}'" if password else ""}
    set protocols bgp neighbor {neighbor_address} solo
    set protocols bgp neighbor {neighbor_address} update-source {neighbor["update-source"]}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast nexthop-self force
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast route-map export {"SIMPLE-IBGP-OUT" if ("simple-out" in neighbor and neighbor["simple-out"]) else route_map_out_name}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast route-map import {route_map_in_name}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast soft-reconfiguration inbound
    """

    return final_filter + bgp_cmd


def get_vyos_protocol_bgp_upstream(neighbor, neighbor_id):
    """cmd to configure vyos protocol bgp upstream"""

    asn = neighbor["asn"]
    neighbor_address = neighbor["neighbor-address"]
    route_map_in_name = f"AS{asn}-UPSTREAM-IN-{neighbor_id}"
    route_map_out_name = f"AS{asn}-UPSTREAM-OUT-{neighbor_id}"

    final_filter = f"""
    delete policy route-map {route_map_in_name}
    set policy route-map {route_map_in_name} rule 10 action permit
    set policy route-map {route_map_in_name} rule 10 call UPSTREAM-IN
    set policy route-map {route_map_in_name} rule 10 on-match next
    set policy route-map {route_map_in_name} rule 200 action permit
    set policy route-map {route_map_in_name} rule 200 on-match next
    set policy route-map {route_map_in_name} rule 1000 action permit

    delete policy route-map {route_map_out_name}
    set policy route-map {route_map_out_name} rule 10 action permit
    set policy route-map {route_map_out_name} rule 10 call UPSTREAM-OUT
    set policy route-map {route_map_out_name} rule 10 on-match next
    set policy route-map {route_map_out_name} rule 200 action permit
    set policy route-map {route_map_out_name} rule 200 on-match next
    set policy route-map {route_map_out_name} rule 1000 action permit
    """

    final_filter += vyos_neighbor_in_optional_attributes(neighbor, route_map_in_name)
    final_filter += vyos_neighbor_out_optional_attributes(neighbor, route_map_out_name)

    ipversion = ipaddress.ip_address(neighbor_address).version

    password = neighbor["password"] if "password" in neighbor else None

    if "multihop" in neighbor:
        if isinstance(neighbor["multihop"], int):
            multihop = neighbor["multihop"]
        else:
            multihop = False
    else:
        multihop = False

    bgp_cmd = f"""
    delete protocols bgp neighbor {neighbor_address}
    set protocols bgp neighbor {neighbor_address} description '{neighbor["description"] if "description" in neighbor else f"AS{asn}-Upstream"}'
    set protocols bgp neighbor {neighbor_address} graceful-restart enable
    set protocols bgp neighbor {neighbor_address} remote-as {asn}
    {f"set protocols bgp neighbor {neighbor_address} password '{password}'" if password else ""}
    {f"set protocols bgp neighbor {neighbor_address} ebgp-multihop {multihop}" if multihop else ""}
    set protocols bgp neighbor {neighbor_address} solo
    set protocols bgp neighbor {neighbor_address} update-source {neighbor["update-source"]}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast nexthop-self force
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast route-map export {route_map_out_name}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast route-map import {route_map_in_name}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast soft-reconfiguration inbound
    """

    return final_filter + bgp_cmd


def get_vyos_protocol_bgp_routeserver(neighbor, neighbor_id):
    """cmd to configure vyos protocol bgp routeserver"""

    asn = neighbor["asn"]
    neighbor_address = neighbor["neighbor-address"]
    route_map_in_name = f"AS{asn}-ROUTESERVER-IN-{neighbor_id}"
    route_map_out_name = f"AS{asn}-ROUTESERVER-OUT-{neighbor_id}"

    final_filter = f"""
    delete policy route-map {route_map_in_name}
    set policy route-map {route_map_in_name} rule 10 action permit
    set policy route-map {route_map_in_name} rule 10 call ROUTESERVER-IN
    set policy route-map {route_map_in_name} rule 10 on-match next
    set policy route-map {route_map_in_name} rule 200 action permit
    set policy route-map {route_map_in_name} rule 200 on-match next
    set policy route-map {route_map_in_name} rule 1000 action permit

    delete policy route-map {route_map_out_name}
    set policy route-map {route_map_out_name} rule 10 action permit
    set policy route-map {route_map_out_name} rule 10 call ROUTESERVER-OUT
    set policy route-map {route_map_out_name} rule 10 on-match next
    set policy route-map {route_map_out_name} rule 200 action permit
    set policy route-map {route_map_out_name} rule 200 on-match next
    set policy route-map {route_map_out_name} rule 1000 action permit
    """

    final_filter += vyos_neighbor_in_optional_attributes(neighbor, route_map_in_name)
    final_filter += vyos_neighbor_out_optional_attributes(neighbor, route_map_out_name)

    ipversion = ipaddress.ip_address(neighbor_address).version

    password = neighbor["password"] if "password" in neighbor else None

    bgp_cmd = f"""
    delete protocols bgp neighbor {neighbor_address}
    set protocols bgp neighbor {neighbor_address} description '{neighbor["description"] if "description" in neighbor else f"AS{asn}-RS"}'
    set protocols bgp neighbor {neighbor_address} graceful-restart enable
    set protocols bgp neighbor {neighbor_address} remote-as {asn}
    {f"set protocols bgp neighbor {neighbor_address} password '{password}'" if password else ""}
    set protocols bgp neighbor {neighbor_address} solo
    set protocols bgp neighbor {neighbor_address} update-source {neighbor["update-source"]}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast nexthop-self force
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast route-map export {route_map_out_name}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast route-map import {route_map_in_name}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast soft-reconfiguration inbound
    """

    return final_filter + bgp_cmd


def get_vyos_protocol_bgp_peer(neighbor, neighbor_id):
    """cmd to configure vyos protocol bgp peer"""

    asn = neighbor["asn"]
    neighbor_address = neighbor["neighbor-address"]
    route_map_in_name = f"AS{asn}-PEER-IN-{neighbor_id}"
    route_map_out_name = f"AS{asn}-PEER-OUT-{neighbor_id}"

    final_filter = f"""
    delete policy route-map {route_map_in_name}
    set policy route-map {route_map_in_name} rule 10 action permit
    set policy route-map {route_map_in_name} rule 10 call PEER-IN
    set policy route-map {route_map_in_name} rule 10 on-match next
    set policy route-map {route_map_in_name} rule 20 action permit
    set policy route-map {route_map_in_name} rule 20 call FILTER-AS{asn}-IN
    set policy route-map {route_map_in_name} rule 20 on-match next
    set policy route-map {route_map_in_name} rule 200 action permit
    set policy route-map {route_map_in_name} rule 200 on-match next
    set policy route-map {route_map_in_name} rule 1000 action permit

    delete policy route-map {route_map_out_name}
    set policy route-map {route_map_out_name} rule 10 action permit
    set policy route-map {route_map_out_name} rule 10 call PEER-OUT
    set policy route-map {route_map_out_name} rule 10 on-match next
    set policy route-map {route_map_out_name} rule 200 action permit
    set policy route-map {route_map_out_name} rule 200 on-match next
    set policy route-map {route_map_out_name} rule 1000 action permit
    """

    final_filter += vyos_neighbor_in_optional_attributes(neighbor, route_map_in_name)
    final_filter += vyos_neighbor_out_optional_attributes(neighbor, route_map_out_name)

    ipversion = ipaddress.ip_address(neighbor_address).version

    password = neighbor["password"] if "password" in neighbor else None

    bgp_cmd = f"""
    delete protocols bgp neighbor {neighbor_address}
    set protocols bgp neighbor {neighbor_address} description '{neighbor["description"] if "description" in neighbor else f"AS{asn}-Peer"}'
    set protocols bgp neighbor {neighbor_address} graceful-restart enable
    set protocols bgp neighbor {neighbor_address} remote-as {asn}
    {f"set protocols bgp neighbor {neighbor_address} password '{password}'" if password else ""}
    set protocols bgp neighbor {neighbor_address} solo
    set protocols bgp neighbor {neighbor_address} update-source {neighbor["update-source"]}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast nexthop-self force
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast route-map export {route_map_out_name}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast route-map import {route_map_in_name}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast soft-reconfiguration inbound
    """

    return final_filter + bgp_cmd


def get_vyos_protocol_bgp_downstream(neighbor, neighbor_id):
    """cmd to configure vyos protocol bgp downstream"""

    asn = neighbor["asn"]
    neighbor_address = neighbor["neighbor-address"]
    route_map_in_name = f"AS{asn}-DOWNSTREAM-IN-{neighbor_id}"
    route_map_out_name = f"AS{asn}-DOWNSTREAM-OUT-{neighbor_id}"

    final_filter = f"""
    delete policy route-map {route_map_in_name}
    set policy route-map {route_map_in_name} rule 10 action permit
    set policy route-map {route_map_in_name} rule 10 call DOWNSTREAM-IN
    set policy route-map {route_map_in_name} rule 10 on-match next
    set policy route-map {route_map_in_name} rule 20 action permit
    set policy route-map {route_map_in_name} rule 20 call FILTER-AS{asn}-IN
    set policy route-map {route_map_in_name} rule 20 on-match next
    set policy route-map {route_map_in_name} rule 200 action permit
    set policy route-map {route_map_in_name} rule 200 on-match next
    set policy route-map {route_map_in_name} rule 1000 action permit

    delete policy route-map {route_map_out_name}
    set policy route-map {route_map_out_name} rule 10 action permit
    set policy route-map {route_map_out_name} rule 10 call DOWNSTREAM-OUT
    set policy route-map {route_map_out_name} rule 10 on-match next
    set policy route-map {route_map_out_name} rule 200 action permit
    set policy route-map {route_map_out_name} rule 200 on-match next
    set policy route-map {route_map_out_name} rule 1000 action permit
    """

    final_filter += vyos_neighbor_in_optional_attributes(neighbor, route_map_in_name)
    final_filter += vyos_neighbor_out_optional_attributes(neighbor, route_map_out_name)

    ipversion = ipaddress.ip_address(neighbor_address).version

    password = neighbor["password"] if "password" in neighbor else None

    if "multihop" in neighbor:
        if isinstance(neighbor["multihop"], int):
            multihop = neighbor["multihop"]
        else:
            multihop = False
    else:
        multihop = False

    bgp_cmd = f"""
    delete protocols bgp neighbor {neighbor_address}
    set protocols bgp neighbor {neighbor_address} description '{neighbor["description"] if "description" in neighbor else f"AS{asn}-Downstream"}'
    set protocols bgp neighbor {neighbor_address} graceful-restart enable
    set protocols bgp neighbor {neighbor_address} remote-as {asn}
    {f"set protocols bgp neighbor {neighbor_address} password '{password}'" if password else ""}
    {f"set protocols bgp neighbor {neighbor_address} ebgp-multihop {multihop}" if multihop else ""}
    set protocols bgp neighbor {neighbor_address} solo
    set protocols bgp neighbor {neighbor_address} update-source {neighbor["update-source"]}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast nexthop-self force
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast route-map export {route_map_out_name}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast route-map import {route_map_in_name}
    set protocols bgp neighbor {neighbor_address} address-family ipv{ipversion}-unicast soft-reconfiguration inbound
    """

    return final_filter + bgp_cmd


def get_vyos_protocol_bgp(bgp_config, _router_id):
    """cmd to configure vyos protocol bgp"""

    cmd = f"""
    delete protocols bgp address-family
    delete protocols bgp parameters
    delete protocols bgp system-as
    set protocols bgp address-family ipv4-unicast redistribute connected
    set protocols bgp address-family ipv4-unicast redistribute static
    set protocols bgp address-family ipv6-unicast redistribute connected
    set protocols bgp address-family ipv6-unicast redistribute static
    set protocols bgp parameters graceful-restart
    set protocols bgp parameters router-id {_router_id}
    set protocols bgp system-as {local_asn}
    """

    for ibgp_neighbor in bgp_config["ibgp"]:
        if "manual" in ibgp_neighbor and ibgp_neighbor["manual"]:
            continue
        nid = bgp_config["ibgp"].index(ibgp_neighbor)
        cmd += get_vyos_protocol_bgp_ibgp(ibgp_neighbor, nid)
    for upstream_neighbor in bgp_config["upstream"]:
        if "manual" in upstream_neighbor and upstream_neighbor["manual"]:
            continue
        this_asn_neighbor_list = [
            n for n in bgp_config["upstream"] if n["asn"] == upstream_neighbor["asn"]
        ]
        nid = this_asn_neighbor_list.index(upstream_neighbor)
        cmd += get_vyos_protocol_bgp_upstream(upstream_neighbor, nid)
    for routeserver_neighbor in bgp_config["routeserver"]:
        if "manual" in routeserver_neighbor and routeserver_neighbor["manual"]:
            continue
        this_asn_neighbor_list = [
            n
            for n in bgp_config["routeserver"]
            if n["asn"] == routeserver_neighbor["asn"]
        ]
        nid = this_asn_neighbor_list.index(routeserver_neighbor)
        cmd += get_vyos_protocol_bgp_routeserver(routeserver_neighbor, nid)
    for peer_neighbor in bgp_config["peer"]:
        if "manual" in peer_neighbor and peer_neighbor["manual"]:
            continue
        this_asn_neighbor_list = [
            n for n in bgp_config["peer"] if n["asn"] == peer_neighbor["asn"]
        ]
        nid = this_asn_neighbor_list.index(peer_neighbor)
        cmd += get_vyos_protocol_bgp_peer(peer_neighbor, nid)
    for downstream_neighbor in bgp_config["downstream"]:
        if "manual" in downstream_neighbor and downstream_neighbor["manual"]:
            continue
        this_asn_neighbor_list = [
            n
            for n in bgp_config["downstream"]
            if n["asn"] == downstream_neighbor["asn"]
        ]
        nid = this_asn_neighbor_list.index(downstream_neighbor)
        cmd += get_vyos_protocol_bgp_downstream(downstream_neighbor, nid)

    return cmd


def get_vyos_set_src(router_config):
    cmd = """
    delete policy route-map SET-SRC
    set policy route-map SET-SRC rule 10 action 'permit'
    set policy route-map SET-SRC rule 10 match ip address prefix-list 'IPv4-ALL'
    set policy route-map SET-SRC rule 20 action 'permit'
    set policy route-map SET-SRC rule 20 match ipv6 address prefix-list 'IPv6-ALL'
    """

    if "src" in router_config:
        if "ipv4" in router_config["src"]:
            cmd += f"""
            set policy route-map SET-SRC rule 10 set src '{router_config["src"]["ipv4"]}'
            delete system ip protocol bgp
            set system ip protocol bgp route-map SET-SRC
            """
        if "ipv6" in router_config["src"]:
            cmd += f"""
            set policy route-map SET-SRC rule 20 set src '{router_config["src"]["ipv6"]}'
            delete system ipv6 protocol bgp
            set system ipv6 protocol bgp route-map SET-SRC
            """

    return cmd


def get_final_vyos_cmd(router_config):
    """cmd to configure vyos"""

    router_name = router_config["name"]
    router_id = get_router_id(router_name)
    if not router_id:
        print(f"resolve router-id for {router_name} failed")
        return ""
    print(f"generate vyos bgp script for {router_name}({router_id})")

    configure = ""

    # local asn prefix list
    configure += get_vyos_prefix_list(
        4, local_asn, filter_name="LOCAL-ASN-CONE", cone=True
    )
    configure += get_vyos_prefix_list(
        6, local_asn, filter_name="LOCAL-ASN-CONE", cone=True
    )
    configure += get_vyos_prefix_list(4, local_asn, filter_name="LOCAL-ASN-PREFIX4")
    configure += get_vyos_prefix_list(6, local_asn, filter_name="LOCAL-ASN-PREFIX6")
    configure += get_vyos_prefix_list(
        4, local_asn, max_length=32, filter_name="LOCAL-ASN-PREFIX4-le32"
    )
    configure += get_vyos_prefix_list(
        6, local_asn, max_length=128, filter_name="LOCAL-ASN-PREFIX6-le128"
    )

    # AS prefix list and AS filter
    upstream_asns = [n["asn"] for n in router_config["protocols"]["bgp"]["upstream"]]
    routeserver_asns = [
        n["asn"] for n in router_config["protocols"]["bgp"]["routeserver"]
    ]
    peer_asns = [n["asn"] for n in router_config["protocols"]["bgp"]["peer"]]
    downstream_asns = [
        n["asn"] for n in router_config["protocols"]["bgp"]["downstream"]
    ]

    # connected_asns = upstream_asns + routeserver_asns + peer_asns + downstream_asns
    # we only need to generate filter for peer and downstream
    connected_asns = peer_asns + downstream_asns
    connected_asns = sorted(list(set(connected_asns)))
    for asn in connected_asns:
        role = []
        if asn in peer_asns:
            role.append("peer")
        if asn in downstream_asns:
            role.append("downstream")
        configure += get_vyos_as_filter(asn, role)

    # protocol rpki
    configure += get_vyos_protocol_rpki(router_config["protocols"]["rpki"])

    # protocol bgp
    configure += get_vyos_protocol_bgp(router_config["protocols"]["bgp"], router_id)

    # set route src
    if "src" in router_config:
        configure += get_vyos_set_src(router_config)

    configure = "\n".join(
        [line.strip() for line in configure.splitlines() if line.strip()]
    )

    configure = (
        "\nconfigure\n"
        + defaultconfig
        + (
            router_config["custom-config"] + "\n"
            if "custom-config" in router_config
            else ""
        )
        + configure
        + "\ncommit\nexit\n"
    )

    #################################

    script_start = r"""#!/bin/vbash

if [ "$(id -g -n)" != 'vyattacfg' ] ; then
    exec sg vyattacfg -c "/bin/vbash $(readlink -f $0) $@"
fi

source /opt/vyatta/etc/functions/script-template
"""
    script_end = r"""exit"""

    #################################

    return script_start + configure + script_end


if __name__ == "__main__":

    if not router_list:
        raise ValueError("router list is empty")

    if not os.path.exists(os.path.join(work_dir, "outputs")):
        os.makedirs(os.path.join(work_dir, "outputs"))

    for router in router_list:
        script = get_final_vyos_cmd(router)
        with open(
            os.path.join(work_dir, "outputs", f"set-bgp.{router['name']}.sh"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(script)
        print(f"set-bgp.{router['name']}.sh generated.")
