"""Expected host settings for Pure FlashArray on RHEL 9.

Defaults follow the Portworx FlashArray Direct Access (FADA) multipath guidance
for RHEL. Pure revises these between releases, so check them against the docs
for the CSI driver version you run. Override any of it with a JSON file named by
PTK_POLICY_FILE (mount it from a ConfigMap); keys you omit keep these defaults.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

DEFAULT_POLICY: Dict[str, Any] = {
    # iscsi | nvme-tcp | fc | any
    "protocol": "iscsi",
    "min_paths": 2,
    "defaults": {
        "user_friendly_names": "no",
        "find_multipaths": "yes",
    },
    "pure_scsi_device": {
        "path_selector": "service-time 0",
        "hardware_handler": "1 alua",
        "path_grouping_policy": "group_by_prio",
        "prio": "alua",
        "failback": "immediate",
        "path_checker": "tur",
        "fast_io_fail_tmo": "10",
        "user_friendly_names": "no",
        "no_path_retry": "0",
        "features": "0",
        "dev_loss_tmo": "600",
    },
    "pure_nvme_device": {
        "path_selector": "queue-length 0",
        "path_grouping_policy": "group_by_prio",
        "prio": "ana",
        "failback": "immediate",
        "fast_io_fail_tmo": "10",
        "user_friendly_names": "no",
        "no_path_retry": "0",
        "features": "0",
        "dev_loss_tmo": "60",
    },
    # vSphere VMs: the OS disk must never be claimed by multipathd.
    "require_blacklist": [{"vendor": "VMware", "product": "Virtual disk"}],
    # Block-layer scheduler for Pure paths. FlashArray does its own scheduling.
    "scheduler": "none",
}


def load_policy() -> Dict[str, Any]:
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    path = os.environ.get("PTK_POLICY_FILE")
    if path and os.path.exists(path):
        with open(path) as f:
            override = json.load(f)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(policy.get(key), dict):
                policy[key].update(value)
            else:
                policy[key] = value
    if os.environ.get("PTK_PROTOCOL"):
        policy["protocol"] = os.environ["PTK_PROTOCOL"]
    return policy
