"""노드 이름 자동 해석. 실제 NAS 에서 관찰한 값들을 그대로 넣는다."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sender.node_name import same_machine, parse_scan, _meaningful   # noqa: E402

fails = []
def check(label, got, want):
    ok = got == want
    print(("  PASS  " if ok else "  FAIL  ") + label + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok: fails.append(label)

AIDAS = {"fqdn": "aidas", "machine_id": "bdbae93d2990fa70"}
B200_0 = {"fqdn": "gpu-0-0", "machine_id": "10433b944b7e6418"}
B200_1 = {"fqdn": "gpu-1-0", "machine_id": "10433b944b7e6418"}
B200_2 = {"fqdn": "gpu-2-0", "machine_id": None}          # 구버전은 mid 를 안 보냄

print("[1] 같은 장비 판정")
check("한 서버의 세 이름은 같은 장비", same_machine(AIDAS, dict(AIDAS)), True)
check("machine_id 가 같아도 fqdn 이 다르면 다른 노드", same_machine(B200_0, B200_1), False)
check("fqdn 이 같고 한쪽에 mid 가 없으면 같은 장비", same_machine(B200_2, {"fqdn": "gpu-2-0", "machine_id": "10433b944b7e6418"}), True)
check("fqdn 이 같아도 mid 가 다르면 다른 장비(딴 클러스터의 동명 파드)",
      same_machine({"fqdn": "gpu-0-0", "machine_id": "aaaa"}, B200_0), False)

print("[2] 쓸 수 없는 이름")
check("역방향 DNS 존은 이름이 아니다", _meaningful("1.0.0.0.0.0.0.0.0.ip6.arpa"), False)
check("숫자뿐인 주소", _meaningful("192.168.1.5"), False)
check("localhost", _meaningful("localhost"), False)
check("빈 값", _meaningful(""), False)
check("이름이 못 미더우면 매칭 자체를 안 한다", same_machine({"fqdn": "localhost"}, {"fqdn": "localhost"}), False)

print("[3] 원격 스캔 파싱")
scan = parse_scan('''== ADS-A100
{"fqdn": "aidas", "ip": "147.47.206.44", "machine_id": "bdbae93d2990fa70", "node_id": "ADS-A100"}

== aidas-a100
 "machine_id": "bdbae93d2990fa70"
 "fqdn": "aidas"

== broken
 (쓰레기)
''')
check("마커는 JSON 으로 읽는다", scan["ADS-A100"]["ident"]["fqdn"], "aidas")
check("마커임을 표시한다", scan["ADS-A100"]["marker"], True)
check("배치 조각도 읽는다", scan["aidas-a100"]["ident"]["machine_id"], "bdbae93d2990fa70")
check("조각은 마커가 아니다", scan["aidas-a100"]["marker"], False)
check("fqdn 이 없으면 후보에서 뺀다", "broken" in scan, False)

print("[4] 이름이 갈라진 장비에서 무엇을 고르는가")
split = parse_scan("""== gpu-2-0
batches=784
 "machine_id": "10433b944b7e6418"
 "fqdn": "gpu-2-0"

== kakao-b200-2
batches=887
 "fqdn": "gpu-2-0"
""")
check("batches 를 읽는다", split["gpu-2-0"]["batches"], 784)
check("구버전이라 mid 가 없어도 같은 장비로 본다",
      same_machine(split["gpu-2-0"]["ident"], split["kakao-b200-2"]["ident"]), True)
hits = sorted(((v["marker"], -v["batches"], n) for n, v in split.items()))
check("배치가 많은 쪽을 고른다(알파벳순 아님)", hits[0][2], "kakao-b200-2")


if fails:
    print(f"실패 {len(fails)}건: {', '.join(fails)}")
    sys.exit(1)
print("전부 통과")
