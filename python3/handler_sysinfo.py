"""System information handler (mixed into Handler)."""

import os
import platform
from tags import jenc
from times import _read_os_release



class HandlerSysinfoMixin:
    """System information handler (mixed into Handler)."""

    def _handle_sysinfo(self):
        system = platform.system()
        info = {
            "system": system,
            "system_name": {"Darwin": "macOS"}.get(system, system),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "hostname": platform.node(),
            "python_version": platform.python_version(),
            "cpu_count": jenc(os.cpu_count()),
            "distro": "null",
            "windows": "null",
            "mac": "null",
        }
        try:
            if system == "Linux":
                osrel = _read_os_release()
                if osrel:
                    info["distro"] = {
                        "id": osrel.get("ID", ""),
                        "name": osrel.get("NAME", ""),
                        "version": osrel.get("VERSION", ""),
                        "version_id": osrel.get("VERSION_ID", ""),
                        "pretty_name": osrel.get("PRETTY_NAME", ""),
                    }
            elif system == "Windows":
                release, version, service_pack, _ptype = platform.win32_ver()
                try:
                    edition = platform.win32_edition() or ""
                except (AttributeError, OSError):
                    edition = ""
                info["windows"] = {
                    "release": release,
                    "version": version,
                    "service_pack": service_pack,
                    "edition": edition,
                }
            elif system == "Darwin":
                mac_release, _, _ = platform.mac_ver()
                info["mac"] = {"version": mac_release}
        except OSError:
            pass
        self._send_json(info)
