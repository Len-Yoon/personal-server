"""Stage Caddy and cloudflared routes for a pinned Windows ingress candidate."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import PureWindowsPath
import re

from . import caddy_cutover, car_callback_ingress
from .controller import TransitionBlocked
from .initial_pg_lease import RECOVERY_OPEN_REVISION
from .native_guest_activation import INITIAL_GENERATION, ServingGeneration


MAIN_ORIGIN_PORT = 18080
CAR_ORIGIN_PORT = 18015
LOOPBACK = "127.0.0.1"
WINDOWS_TLS = "C:/ProgramData/N100HA/ingress/tls"
ROUTE_HEADER = "X-N100-Ingress-Route"

LEGACY_OPTIONS = "{\n    email {env.CADDY_EMAIL}\n}\n"
LEGACY_TLS = """(common_tls) {
    tls {
        dns cloudflare {env.CLOUDFLARE_API_TOKEN}
        resolvers 1.1.1.1 1.0.0.1
    }
    encode zstd gzip
}
"""
UPLOAD_LIMIT = """(portal_file_upload_limit) {
    @file_uploads path /files/uploads
    request_body @file_uploads {
        max_size 30MB
    }
}
"""
NATIVE_OPTIONS = """{
    admin off
    auto_https off
    persist_config off
    log {
        output discard
    }
}
"""
NATIVE_ENCODING = "(common_tls) {\n    encode zstd gzip\n}\n"
PUBLIC_ROUTES = {
    "portal.len.pe.kr": None,
    "len.pe.kr": "PORTAL_UPSTREAM",
    "portfolio.len.pe.kr": "PORTAL_UPSTREAM",
    "file.len.pe.kr": "PORTAL_UPSTREAM",
    "admin.len.pe.kr": "PORTAL_UPSTREAM",
    "news.len.pe.kr": "CRAWLER_WORKER_UPSTREAM",
    "memo.len.pe.kr": "YOUTUBE_MEMO_UPSTREAM",
    "books.len.pe.kr": "BOOK_MEMO_UPSTREAM",
}
PIN = re.compile(r"[a-f0-9]{64}")
SITE = re.compile(
    r"(?P<host>[a-z0-9.-]+) \{\n"
    r"    import common_tls\n"
    r"(?P<limit>    import portal_file_upload_limit\n)?"
    r"    (?:reverse_proxy \{env\.(?P<upstream>[A-Z_]+)\}|redir https://len\.pe\.kr\{uri\} permanent)\n"
    r"\}\n(?:\n|$)"
)


@dataclass(frozen=True)
class WindowsIngressCandidate:
    caddyfile: bytes
    tunnel_ingress: bytes
    route_pin: str


_WINDOWS_FRONTEND_ENTRY = r"""$ErrorActionPreference='Stop';
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class HaFrontendDirectoryLease {
    [StructLayout(LayoutKind.Sequential)]
    private struct FileInfo {
        public uint Attributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME Created, Accessed, Written;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    private static extern SafeFileHandle CreateFileW(string path, uint access, uint sharing, IntPtr security,
                                                    uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInfo info);

    public static SafeFileHandle Open(string path) {
        SafeFileHandle handle=CreateFileW(path,0x20000,1,IntPtr.Zero,3,0x02200000,IntPtr.Zero);
        FileInfo info;
        if (handle.IsInvalid || !GetFileInformationByHandle(handle,out info)
            || (info.Attributes & 0x410) != 0x10) {
            handle.Dispose();
            throw new InvalidOperationException("native_frontend_directory_unconfirmed");
        }
        return handle;
    }
}
'@;
function Assert-FrontendAcl($Entry,[bool]$Ancestor) {
    if($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'native_frontend_reparse'};
    $descriptor=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $Entry.FullName).Sddl);
    $writers=@('S-1-5-18','S-1-5-32-544');
    $trustedRoot=$Ancestor -and $Entry.FullName -ieq 'C:\' -and
        $descriptor.Owner.Value -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464';
    if($null -eq $descriptor.Owner -or ($descriptor.Owner.Value -notin $writers -and -not $trustedRoot) -or
       $null -eq $descriptor.DiscretionaryAcl -or $descriptor.DiscretionaryAcl.Count -eq 0){throw 'native_frontend_acl'};
    foreach($ace in $descriptor.DiscretionaryAcl){
        if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'native_frontend_acl'};
        if([int]$ace.AceFlags -band 8){continue};
        if($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed){
            $mask=[long]$ace.AccessMask -band 4294967295;
            $writeMask=if($Ancestor){269287488}else{1343029590};
            if(($mask -band $writeMask) -ne 0 -and $ace.SecurityIdentifier.Value -notin $writers){throw 'native_frontend_writer'};
            if(-not $Ancestor -and ($mask -band 2415919105) -ne 0 -and $ace.SecurityIdentifier.Value -notin $writers){throw 'native_frontend_reader'};
        }
    }
};
$directories=[Collections.Generic.List[object]]::new();
function Hold-FrontendParents([string]$Path) {
    $entry=Get-Item -LiteralPath $Path -Force;
    Assert-FrontendAcl $entry $false;
    $parent=$entry.Directory;
    while($null -ne $parent){
        $ancestor=$parent.FullName -ieq 'C:\ProgramData' -or $parent.FullName -ieq 'C:\';
        Assert-FrontendAcl $parent $ancestor;
        $directories.Add([HaFrontendDirectoryLease]::Open($parent.FullName));
        Assert-FrontendAcl (Get-Item -LiteralPath $parent.FullName -Force) $ancestor;
        $parent=$parent.Parent;
    }
};
$file=$null;
try {
  Hold-FrontendParents 'SCRIPT_PATH';
  Hold-FrontendParents 'REQUEST_PATH';
  $file=[IO.File]::Open('SCRIPT_PATH',[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read);
  $h=[Security.Cryptography.SHA256]::Create();
  try {$pin=[BitConverter]::ToString($h.ComputeHash($file)).Replace('-','').ToLowerInvariant()} finally {$h.Dispose()};
  if($pin -cne 'SCRIPT_SHA'){throw 'native_frontend_script_changed'};
  & 'SCRIPT_PATH' -Mode MODE_NAME -RequestPath 'REQUEST_PATH' -RequestSha256 REQUEST_SHA -ExpectedProfileSha256 PROFILE_SHA -StageScriptSha256 STAGE_SHA;
} finally {
  if($null -ne $file){$file.Dispose()};
  foreach($directory in $directories){$directory.Dispose()};
}
"""

class WindowsNativeFrontend:
    """Invoke a pinned, protected Windows-local lifecycle script under the caller's guard."""

    MODES = frozenset({"Install", "StartCaddy", "StartTunnel", "Observe", "Rollback",
                       "StartCaddyMaintenance", "StartTunnelMaintenance", "ObserveMaintenance"})

    def __init__(self, windows, *, script_path, script_sha256, stage_script_sha256,
                 request_path, request_sha256, profile_sha256):
        values = (script_sha256, stage_script_sha256, request_sha256, profile_sha256)
        if any(type(pin) is not str or not PIN.fullmatch(pin) for pin in values):
            raise TransitionBlocked("native_frontend_request_invalid")
        for path in (script_path, request_path):
            if (type(path) is not str or len(path) > 240 or not path.isascii()
                    or not path.startswith("C:\\ProgramData\\") or str(PureWindowsPath(path)) != path
                    or any(character in path for character in "'\"$`;*?<>|") or ":" in path[3:]
                    or any(ord(character) < 32 for character in path)
                    or any(part in {".", ".."} or part.endswith((".", " ")) for part in path[3:].split("\\"))):
                raise TransitionBlocked("native_frontend_request_invalid")
        self.windows = windows
        self.script_path = script_path
        self.script_sha256 = script_sha256
        self.stage_script_sha256 = stage_script_sha256
        self.request_path = request_path
        self.request_sha256 = request_sha256
        self.profile_sha256 = profile_sha256

    def run(self, mode, deadline, *, assert_guard):
        if type(mode) is not str or mode not in self.MODES or not callable(assert_guard):
            raise TransitionBlocked("native_frontend_request_invalid")
        if assert_guard() is not True:
            raise TransitionBlocked("native_frontend_guard_unconfirmed")
        parameters = {"SCRIPT_PATH": self.script_path, "REQUEST_PATH": self.request_path,
                      "SCRIPT_SHA": self.script_sha256, "MODE_NAME": mode, "REQUEST_SHA": self.request_sha256,
                      "STAGE_SHA": self.stage_script_sha256, "PROFILE_SHA": self.profile_sha256}
        source = re.sub(r"SCRIPT_PATH|REQUEST_PATH|SCRIPT_SHA|MODE_NAME|REQUEST_SHA|STAGE_SHA|PROFILE_SHA",
                        lambda match: parameters[match.group()], _WINDOWS_FRONTEND_ENTRY)
        result = self.windows.powershell(source, deadline)
        maintenance = mode.endswith("Maintenance")
        expected_keys = {"schema_version", "status", "mode", "request_sha256", "profile_sha256", "caddy_state",
                         "tunnel_state", "production_external_verified", "credentials_copied"}
        if maintenance:
            expected_keys |= {"application_ready", "origin_status_codes"}
        if (type(result) is not dict or set(result) != expected_keys or result.get("schema_version") != 1
                or type(result.get("schema_version")) is not int
                or result.get("status") != ("MAINTENANCE" if maintenance else "PASS") or result.get("mode") != mode
                or result.get("request_sha256") != self.request_sha256
                or result.get("profile_sha256") != self.profile_sha256
                or result.get("credentials_copied") is not False or result.get("production_external_verified") is not False
                or assert_guard() is not True):
            raise TransitionBlocked("native_frontend_result_unconfirmed")
        if maintenance:
            origins = result.get("origin_status_codes")
            expected_origins = {host + path for host, upstream in PUBLIC_ROUTES.items() if upstream
                                for path in ("/ready", "/health")} | {car_callback_ingress.PUBLIC_HOST + "/health"}
            if (result.get("application_ready") is not False or type(origins) is not dict
                    or set(origins) != expected_origins
                    or any(type(code) is not int or code not in {200, 503} for code in origins.values())):
                raise TransitionBlocked("native_frontend_result_unconfirmed")
        if any(result.get(key) not in {"Absent", "Stopped", "Running"} for key in ("caddy_state", "tunnel_state")):
            raise TransitionBlocked("native_frontend_result_unconfirmed")
        expected = {"Install": ("Stopped", "Stopped"), "StartCaddy": ("Running", "Stopped"),
                    "StartTunnel": ("Running", "Running"), "StartCaddyMaintenance": ("Running", "Stopped"),
                    "StartTunnelMaintenance": ("Running", "Running"), "ObserveMaintenance": ("Running", "Running")}
        if mode in expected and (result.get("caddy_state"), result.get("tunnel_state")) != expected[mode]:
            raise TransitionBlocked("native_frontend_result_unconfirmed")
        if mode == "Rollback" and any(result.get(key) not in {"Absent", "Stopped"} for key in ("caddy_state", "tunnel_state")):
            raise TransitionBlocked("native_frontend_result_unconfirmed")
        return result


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _validate_routes(source):
    prefix = "\n".join((LEGACY_OPTIONS, LEGACY_TLS, UPLOAD_LIMIT, ""))
    if not source.startswith(prefix):
        raise TransitionBlocked("native_ingress_source_shape_changed")

    hosts = set()
    position = len(prefix)
    while position < len(source):
        block = SITE.match(source, position)
        if block is None:
            raise TransitionBlocked("native_ingress_source_shape_changed")
        host, limit, upstream = block.groups()
        if (host in hosts or host not in PUBLIC_ROUTES or PUBLIC_ROUTES[host] != upstream
                or bool(limit) != (upstream == "PORTAL_UPSTREAM")):
            raise TransitionBlocked("native_ingress_host_contract_changed")
        hosts.add(host)
        position = block.end()

    if hosts != PUBLIC_ROUTES.keys():
        raise TransitionBlocked("native_ingress_host_contract_changed")


def render_candidate(source, *, source_sha256, active_ip, generation, plan_sha256, tls_files_sha256,
                     recovery_attempt_id=None):
    """Produce only route files. The caller must independently verify live authority and mTLS files."""
    if (type(generation) is not ServingGeneration or type(active_ip) is not str
            or any(type(pin) is not str or not PIN.fullmatch(pin) for pin in (plan_sha256, tls_files_sha256))):
        raise TransitionBlocked("native_ingress_scope_invalid")
    if recovery_attempt_id is None:
        generation.validate()
    elif (type(recovery_attempt_id) is not str or not PIN.fullmatch(recovery_attempt_id)
          or generation.primary != INITIAL_GENERATION.primary
          or any(type(value) is not int for value in (generation.epoch, generation.timeline, generation.revision))
          or (generation.epoch, generation.timeline, generation.revision)
          != (INITIAL_GENERATION.epoch, INITIAL_GENERATION.timeline, RECOVERY_OPEN_REVISION)):
        raise TransitionBlocked("native_ingress_recovered_generation_invalid")

    main = caddy_cutover.render(source, source_sha256, active_ip).decode("utf-8").replace("\r\n", "\n")
    _validate_routes(source.decode("utf-8").replace("\r\n", "\n"))
    pin = sha256(_canonical({"generation": asdict(generation), "plan_sha256": plan_sha256,
                             "source_sha256": source_sha256, "active_ip": active_ip,
                             "tls_files_sha256": tls_files_sha256,
                             "recovery_attempt_id": recovery_attempt_id})).hexdigest()

    main = main.replace(LEGACY_OPTIONS, NATIVE_OPTIONS, 1).replace(LEGACY_TLS, NATIVE_ENCODING, 1)
    marker = "\n".join([
        "    @native_health {", "        method GET", "        path /health", "    }",
        "    header @native_health {", f"        {ROUTE_HEADER} {pin}",
        "        Cache-Control no-store", "        match status 200 503", "    }",
    ])
    for host in sorted(PUBLIC_ROUTES):
        main = main.replace(f"\n{host} {{\n", f"\nhttp://{host}:{MAIN_ORIGIN_PORT} {{\n"
                            f"    bind {LOOPBACK}\n{marker}\n", 1)
    main += f"\nhttp://:{MAIN_ORIGIN_PORT} {{\n    bind {LOOPBACK}\n    respond 404\n}}\n"

    car_host = car_callback_ingress.PUBLIC_HOST
    _, car = car_callback_ingress.render(active_ip, route_pin=pin).decode("ascii").split("\n\n", 1)
    car = car.replace("        match status 200\n", "        match status 200 503\n", 1)
    car = car.replace(f":8015 {{\n", f":{CAR_ORIGIN_PORT} {{\n    bind {LOOPBACK}\n")
    config = (main + "\n" + car).replace(caddy_cutover.MOUNT, WINDOWS_TLS).encode("ascii")

    ingress = [
        {"hostname": host, "service": f"http://{LOOPBACK}:{port}", "originRequest": {"httpHostHeader": host}}
        for host, port in [(host, MAIN_ORIGIN_PORT) for host in sorted(PUBLIC_ROUTES)] + [(car_host, CAR_ORIGIN_PORT)]
    ]
    ingress.append({"service": "http_status:404"})
    return WindowsIngressCandidate(config, _canonical({"ingress": ingress}) + b"\n", pin)
