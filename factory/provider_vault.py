"""Workspace-scoped references for the OS vault or isolated service credentials."""

import ctypes as c
import hashlib
import os
import re
import secrets
import stat
from ctypes import wintypes as w
from pathlib import Path


class VaultError(ValueError):
    pass


class Credential(c.Structure):
    _fields_ = [("Flags", w.DWORD), ("Type", w.DWORD), ("TargetName", w.LPWSTR),
                ("Comment", w.LPWSTR), ("LastWritten", w.FILETIME),
                ("CredentialBlobSize", w.DWORD), ("CredentialBlob", c.POINTER(c.c_ubyte)),
                ("Persist", w.DWORD), ("AttributeCount", w.DWORD),
                ("Attributes", c.c_void_p), ("TargetAlias", w.LPWSTR), ("UserName", w.LPWSTR)]


class WindowsVault:
    def __init__(self, root):
        workspace = hashlib.sha256(os.fsencode(os.path.normcase(root.resolve()))).hexdigest()[:20]
        self.prefix = f"gta-media-factory-local-provider-{workspace}-"
        self.available = os.name == "nt"
        if self.available:
            self.api = c.WinDLL("Advapi32", use_last_error=True)
            self.api.CredWriteW.argtypes = [c.POINTER(Credential), w.DWORD]
            self.api.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(c.POINTER(Credential))]
            self.api.CredDeleteW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD]
            self.api.CredFree.argtypes = [c.c_void_p]
            self.api.CredFree.restype = None

    def target(self, reference):
        if not self.available:
            raise VaultError("Credential storage requires Windows Credential Manager on this build.")
        return self.prefix + reference

    def put(self, reference, secret):
        target = self.target(reference)
        data = secret.encode()
        blob = (c.c_ubyte * len(data)).from_buffer_copy(data)
        entry = Credential(Type=1, TargetName=target, CredentialBlobSize=len(data), CredentialBlob=blob,
                           Persist=2, Comment="Stream Clipping Factory local provider credential",
                           UserName="GTA VI Media Factory")
        try:
            if not self.api.CredWriteW(c.byref(entry), 0):
                raise VaultError("Windows Credential Manager could not save this key.")
        finally:
            c.memset(blob, 0, len(data))

    def get(self, reference):
        target = self.target(reference)
        result = c.POINTER(Credential)()
        if not self.api.CredReadW(target, 1, 0, c.byref(result)):
            if c.get_last_error() == 1168:
                return None
            raise VaultError("Windows Credential Manager could not read this key.")
        try:
            return c.string_at(result.contents.CredentialBlob, result.contents.CredentialBlobSize).decode()
        finally:
            self.api.CredFree(result)

    def remove(self, reference):
        target = self.target(reference)
        if not self.api.CredDeleteW(target, 1, 0) and c.get_last_error() != 1168:
            raise VaultError("Windows Credential Manager could not remove this key.")


class ServiceVault:
    """Restricted, factory-owned POSIX directory on the service host."""

    def __init__(self, root, directory):
        self.path = Path(directory)
        self.prefix = "gta-factory-" + hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:20] + "-"

    def directory(self):
        fd = os.open(self.path,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(fd)
            raise VaultError("The service credential directory must be owned by the factory user with mode 0700.")
        return fd

    @property
    def available(self):
        try:
            os.close(self.directory())
        except (OSError,VaultError):
            return False
        return True

    def target(self, reference):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,90}",reference or ""):
            raise VaultError("Invalid service credential reference.")
        return self.prefix+reference

    def get(self, reference):
        try:
            directory = self.directory()
            try:
                try:
                    fd = os.open(self.target(reference),os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=directory)
                except FileNotFoundError:
                    return None
                with os.fdopen(fd,"rb") as stream:
                    info = os.fstat(stream.fileno())
                    if (info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600
                            or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1):
                        raise VaultError("A factory credential entry has unsafe permissions or type.")
                    result = stream.read(4097)
                    if not 0<len(result)<=4096:
                        raise VaultError("A factory credential entry has an invalid size.")
                    return result.decode("utf-8")
            finally:
                os.close(directory)
        except (OSError,UnicodeError):
            raise VaultError("The factory service could not read this credential.") from None

    def put(self, reference, secret):
        target = self.target(reference)
        raw = secret.encode("utf-8")
        if not 0<len(raw)<=4096:
            raise VaultError("The credential has an invalid size.")
        temporary = self.prefix + "pending-" + secrets.token_hex(12)
        try:
            directory = self.directory()
            try:
                fd = os.open(temporary,os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600,dir_fd=directory)
                with os.fdopen(fd,"wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary,target,src_dir_fd=directory,dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary,dir_fd=directory)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(directory)
        except OSError:
            raise VaultError("The factory service could not store this credential.") from None

    def remove(self, reference):
        target = self.target(reference)
        try:
            directory = self.directory()
            try:
                os.unlink(target,dir_fd=directory)
                os.fsync(directory)
            except FileNotFoundError:
                pass
            finally:
                os.close(directory)
        except OSError:
            raise VaultError("The factory service could not remove this credential.") from None


def vault_for(root):
    if os.name=="nt":
        return WindowsVault(root)
    return ServiceVault(root,os.environ.get("GTA_FACTORY_CREDENTIAL_DIR","/etc/gta-streamer-factory/credentials"))
