"""Builds a fake /host tree resembling a RHEL 9 vSphere VM with Pure iSCSI paths."""

import os

from ptk.policy import DEFAULT_POLICY

GOOD_CONF = "defaults {\n user_friendly_names no\n find_multipaths yes\n}\ndevices {\n device {\n" + "".join(
    f'  {k} "{v}"\n' for k, v in [("vendor", "PURE"), ("product", "FlashArray"),
                                   *DEFAULT_POLICY["pure_scsi_device"].items()]
) + ' }\n}\nblacklist {\n device {\n vendor "VMware"\n product "Virtual disk"\n }\n}\n'

WWID = "3624a93701234567890abcdef00011122"


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def build(root, conf=GOOD_CONF, path_states=("running", "running"), processes=("multipathd", "iscsid"),
          scheduler="[none] mq-deadline"):
    host = os.path.join(root, "host")
    proc = os.path.join(root, "proc")
    write(host, "etc/multipath.conf", conf)
    write(host, "etc/iscsi/initiatorname.iscsi", "InitiatorName=iqn.1994-05.com.redhat:abc123\n")
    write(host, "etc/machine-id", "0123456789abcdef\n")
    write(proc, "modules", "dm_multipath 45056 1 - Live 0x0\nscsi_dh_alua 20480 0 - Live 0x0\n"
                           "iscsi_tcp 24576 2 - Live 0x0\n")
    for i, name in enumerate(processes, start=100):
        write(proc, f"{i}/comm", name + "\n")
    write(host, "sys/block/sda/device/vendor", "VMware  \n")  # OS disk
    slaves = []
    for i, state in enumerate(path_states):
        sd = f"sd{chr(ord('b') + i)}"
        write(host, f"sys/block/{sd}/device/vendor", "PURE    \n")
        write(host, f"sys/block/{sd}/device/state", state + "\n")
        write(host, f"sys/block/{sd}/queue/scheduler", scheduler + "\n")
        slaves.append(sd)
    write(host, "sys/block/dm-3/dm/uuid", f"mpath-{WWID}\n")
    write(host, "sys/block/dm-3/dm/name", WWID + "\n")
    for sd in slaves:
        write(host, f"sys/block/dm-3/slaves/{sd}", "")
    return host, proc
