"""Bind a replacement NIC move to the coordinator's current Windows fence."""
from . import native_stdio_bridge as bridge
from .native_windows_ssh import fixed_script


class WindowsReplacementNetwork:
    def __init__(self, windows, script_sha256, *, primary_vm_id, old_disk_paths_sha256,
                 isolated_switch_id, serving_switch_id, replacement_mac):
        self.windows = windows
        self.script_sha256 = script_sha256
        self.pins = {
            "PrimaryVmId": primary_vm_id,
            "OldDiskPathsSha256": old_disk_paths_sha256,
            "IsolatedSwitchId": isolated_switch_id,
            "ServingSwitchId": serving_switch_id,
            "ReplacementMac": replacement_mac,
        }

    def _parameters(self, replacement, old_vm_id, preflight):
        error = "transition_replacement_network_unconfirmed"
        bridge._need(type(preflight) is bridge.Request and preflight.verb == bridge.GUEST_PREFLIGHT_REPLACEMENT
                     and preflight.node == replacement["node"]
                     and preflight.args["installation_id"] == replacement["installation_id"], error)
        return self.pins | {
            "OldVmId": old_vm_id, "ReplacementVmId": replacement["vm_id"],
            "ReplacementDiskPathsSha256": replacement["disk_paths_sha256"],
        }

    def assert_crash_source_off(self, old_vm_id, deadline):
        """Read the exact old VM and pinned disks without dispatching a stop."""
        error = "transition_crash_source_unconfirmed"
        bridge._need(type(old_vm_id) is str and old_vm_id != self.pins["PrimaryVmId"]
                     and bridge._hash(self.pins["OldDiskPathsSha256"]), error)
        parameters = {"ReadCrashSource": True, "OldVmId": old_vm_id,
                      "OldDiskPathsSha256": self.pins["OldDiskPathsSha256"]}
        source = fixed_script("windows-ha-replacement-network.ps1", self.script_sha256, parameters)
        result = self.windows.powershell(source, deadline)
        bridge._need(bridge._matches(result, {"vm_id": old_vm_id,
                     "disk_paths_sha256": self.pins["OldDiskPathsSha256"],
                     "state": "Off", "automatic_start": "Nothing"}), error)
        bridge._remaining(deadline)
        return True

    def preflight(self, replacement, old_vm_id, request, deadline):
        parameters = self._parameters(replacement, old_vm_id, request)
        source = fixed_script("windows-ha-replacement-network.ps1", self.script_sha256,
                              parameters | {"ObserveOnly": True})
        result = self.windows.powershell(source, deadline)
        bridge._need(bridge._matches(result, {"isolated": True, "vm_id": replacement["vm_id"],
                                             "switch_id": self.pins["IsolatedSwitchId"]}),
                     "transition_replacement_network_unconfirmed")
        return True

    def __call__(self, replacement, old_vm_id, preflight, stage, deadline, *, assert_fenced):
        error = "transition_replacement_network_unconfirmed"
        parameters = self._parameters(replacement, old_vm_id, preflight)
        bridge._need(bridge._hash(stage), error)
        source = fixed_script("windows-ha-replacement-network.ps1", self.script_sha256,
                              parameters | {"StageSha256": stage, "Go": True})
        bridge._need(assert_fenced(deadline) == stage, error)
        result = self.windows.powershell(source, deadline)
        bridge._need(bridge._matches(result, {"attached": True, "stage_sha256": stage,
                                             "vm_id": replacement["vm_id"], "switch_id": self.pins["ServingSwitchId"]}), error)
        bridge._need(assert_fenced(deadline) == stage, error)
        return True
