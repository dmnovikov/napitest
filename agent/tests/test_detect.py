"""Распознавание SD-NAND на поддельных sysfs-данных: запись в microSD или неопознанный
накопитель невозможна при любой нумерации /dev/mmcblk*."""

from napi_factory_agent.fakesys import (FakeDisk, FakePart, build_fake_sys, mountinfo_line, napi_default,
                                        standard_mountinfo)
from napi_factory_agent.hardware_detect import detect_storage, parse_mountinfo, scan_blocks

RULES = {"expected_controller": "ff490000.mmc", "require_controller_match": True,
         "size_min_bytes": 3_500_000_000, "size_max_bytes": 4_400_000_000, "allowed_types": ["MMC", "SD"]}


def test_default_layout(tmp_path):
    root, mi = napi_default(tmp_path)
    inv = detect_storage(root, mi, RULES)
    assert inv["ok"], inv["reasons"]
    assert inv["target"] == "mmcblk0"
    assert inv["target_dev"] == "179:0"
    assert inv["boot_device"] == "mmcblk1"
    dev = {d["name"]: d for d in inv["devices"]}
    assert dev["mmcblk1"]["is_boot"] and dev["mmcblk1"]["controller"] == "ff480000.mmc"
    assert dev["mmcblk0"]["by_path"] == "platform-ff490000.mmc"


def test_swapped_numbers_same_target(tmp_path):
    root, mi = napi_default(tmp_path, swap_numbers=True)
    inv = detect_storage(root, mi, RULES)
    assert inv["ok"]
    assert inv["target"] == "mmcblk1"          # номер другой, контроллер тот же
    assert inv["boot_device"] == "mmcblk0"


def test_boot_from_sdnand_refused(tmp_path):
    # Плата загрузилась с SD-NAND (порядок загрузки не тот): цели нет
    nand = FakeDisk("mmcblk0", "179:0", "ff490000.mmc", partitions=[FakePart("mmcblk0p1", "179:1")])
    sd = FakeDisk("mmcblk1", "179:32", "ff480000.mmc", type="SD", size_bytes=15_931_539_456)
    build_fake_sys(tmp_path, [nand, sd])
    inv = detect_storage(tmp_path, standard_mountinfo("179:1", "/dev/mmcblk0p1"), RULES)
    assert not inv["ok"] and inv["target"] is None
    assert any("источник загрузки" in r for r in inv["reasons"])


def test_mounted_partition_refused(tmp_path):
    nand = FakeDisk("mmcblk0", "179:0", "ff490000.mmc", partitions=[FakePart("mmcblk0p1", "179:1")])
    sd = FakeDisk("mmcblk1", "179:32", "ff480000.mmc", type="SD", size_bytes=15_931_539_456,
                  partitions=[FakePart("mmcblk1p1", "179:33")])
    build_fake_sys(tmp_path, [nand, sd])
    mi = standard_mountinfo("179:33", "/dev/mmcblk1p1") + mountinfo_line(40, "179:1", "/mnt/x", "/dev/mmcblk0p1")
    inv = detect_storage(tmp_path, mi, RULES)
    assert not inv["ok"]
    assert any("смонтировано" in r for r in inv["reasons"])


def test_mounted_via_proc_mounts_only(tmp_path):
    nand = FakeDisk("mmcblk0", "179:0", "ff490000.mmc", partitions=[FakePart("mmcblk0p1", "179:1")])
    sd = FakeDisk("mmcblk1", "179:32", "ff480000.mmc", type="SD", size_bytes=15_931_539_456,
                  partitions=[FakePart("mmcblk1p1", "179:33")])
    build_fake_sys(tmp_path, [nand, sd])
    inv = detect_storage(tmp_path, standard_mountinfo("179:33", "/dev/mmcblk1p1"), RULES,
                         mounts="/dev/mmcblk0p1 /media vfat rw 0 0\n")
    assert not inv["ok"]


def test_readonly_refused(tmp_path):
    nand = FakeDisk("mmcblk0", "179:0", "ff490000.mmc", ro=True)
    sd = FakeDisk("mmcblk1", "179:32", "ff480000.mmc", type="SD", size_bytes=15_931_539_456,
                  partitions=[FakePart("mmcblk1p1", "179:33")])
    build_fake_sys(tmp_path, [nand, sd])
    inv = detect_storage(tmp_path, standard_mountinfo("179:33", "/dev/mmcblk1p1"), RULES)
    assert not inv["ok"]
    assert any("read-only" in r for r in inv["reasons"])


def test_wrong_size_refused(tmp_path):
    root, mi = napi_default(tmp_path, sdnand_size=1_900_000_000)
    inv = detect_storage(root, mi, RULES)
    assert not inv["ok"]
    assert any("размер" in r for r in inv["reasons"])


def test_microsd_with_nand_size_on_wrong_controller(tmp_path):
    # microSD того же объёма, что SD-NAND: отличается только контроллером и тем, что она загрузочная
    root, mi = napi_default(tmp_path, microsd_size=3_909_091_328)
    inv = detect_storage(root, mi, RULES)
    assert inv["ok"] and inv["target"] == "mmcblk0"


def test_two_candidates_ambiguous(tmp_path):
    a = FakeDisk("mmcblk0", "179:0", "ff490000.mmc", host="mmc0")
    b = FakeDisk("mmcblk2", "179:64", "ff490000.mmc", host="mmc2")
    sd = FakeDisk("mmcblk1", "179:32", "ff480000.mmc", type="SD", size_bytes=15_931_539_456,
                  partitions=[FakePart("mmcblk1p1", "179:33")])
    build_fake_sys(tmp_path, [a, b, sd])
    inv = detect_storage(tmp_path, standard_mountinfo("179:33", "/dev/mmcblk1p1"), RULES)
    assert not inv["ok"] and inv["target"] is None
    assert any("неоднозначно" in r for r in inv["reasons"])


def test_no_controller_rule_refused(tmp_path):
    root, mi = napi_default(tmp_path)
    inv = detect_storage(root, mi, {**RULES, "expected_controller": None})
    assert not inv["ok"]
    assert any("контроллер не задан" in r for r in inv["reasons"])


def test_unexpected_controller_refused(tmp_path):
    root, mi = napi_default(tmp_path)
    inv = detect_storage(root, mi, {**RULES, "expected_controller": "ff4a0000.mmc"})
    assert not inv["ok"]


def test_no_sdnand(tmp_path):
    sd = FakeDisk("mmcblk0", "179:0", "ff480000.mmc", type="SD", size_bytes=15_931_539_456,
                  partitions=[FakePart("mmcblk0p1", "179:1")])
    build_fake_sys(tmp_path, [sd])
    inv = detect_storage(tmp_path, standard_mountinfo("179:1", "/dev/mmcblk0p1"), RULES)
    assert not inv["ok"]
    assert any("не найдена" in r for r in inv["reasons"])


def test_root_by_partuuid_resolved_via_majmin(tmp_path):
    # В mountinfo источник может быть /dev/root или PARTUUID — решает только major:minor
    root, _ = napi_default(tmp_path)
    mi = "22 1 179:33 / / rw,relatime shared:1 - ext4 /dev/root rw\n"
    inv = detect_storage(root, mi, RULES)
    assert inv["ok"] and inv["boot_device"] == "mmcblk1"
    mi2 = "22 1 179:33 / / rw,relatime shared:1 - ext4 PARTUUID=1234abcd-02 rw\n"
    assert detect_storage(root, mi2, RULES)["target"] == "mmcblk0"


def test_unknown_root_refused(tmp_path):
    # Корень на overlay/NFS: источник загрузки не определён — никакой записи
    root, _ = napi_default(tmp_path)
    mi = "22 1 0:44 / / rw shared:1 - overlay overlay rw\n"
    inv = detect_storage(root, mi, RULES)
    assert not inv["ok"]
    assert any("не определён" in r for r in inv["reasons"])


def test_boot_partitions_not_candidates(tmp_path):
    root, mi = napi_default(tmp_path)
    # mmcblk0boot0 у eMMC — служебная область, не должна стать вторым кандидатом
    extra = FakeDisk("mmcblk0boot0", "179:8", "ff490000.mmc", host="mmc0", size_bytes=3_909_091_328)
    build_fake_sys(root, [extra])
    names = [b.name for b in scan_blocks(root)]
    assert "mmcblk0boot0" not in names
    assert detect_storage(root, mi, RULES)["ok"]


def test_type_not_allowed(tmp_path):
    root, mi = napi_default(tmp_path)
    (root / "sys/devices/platform/ff490000.mmc/mmc_host/mmc0/mmc0:0001/type").write_text("SDIO\n")
    assert not detect_storage(root, mi, RULES)["ok"]


def test_parse_mountinfo():
    rows = parse_mountinfo("36 35 98:0 /mnt1 /mnt2 rw,noatime master:1 - ext3 /dev/root rw,errors=continue\n")
    assert rows[0]["majmin"] == "98:0" and rows[0]["mountpoint"] == "/mnt2" and rows[0]["source"] == "/dev/root"
