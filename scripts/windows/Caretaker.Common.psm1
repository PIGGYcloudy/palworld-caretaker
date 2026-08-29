Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-CaretakerConfig {
    param([Parameter(Mandatory)][string]$ConfigDir)
    $values = @{}
    foreach ($name in @('caretaker.env', 'server.env', 'secrets.env')) {
        $path = Join-Path $ConfigDir $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        $lineNumber = 0
        foreach ($line in [System.IO.File]::ReadAllLines($path)) {
            $lineNumber++
            $trimmed = $line.Trim()
            if (!$trimmed -or $trimmed.StartsWith('#')) { continue }
            if ($trimmed -notmatch '^(?<key>[A-Z][A-Z0-9_]*)\s*=\s*(?<value>.*)$') {
                throw "${path}:${lineNumber}: expected KEY=VALUE"
            }
            $value = $Matches.value.Trim()
            if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $values[$Matches.key] = $value
        }
    }
    if ($values.Count -eq 0) { throw "No deployment configuration file found in $ConfigDir" }
    return $values
}

function Get-ConfigValue {
    param([hashtable]$Config, [string]$Name, [string]$Default = '')
    if ($Config.ContainsKey($Name)) { return [string]$Config[$Name] }
    return $Default
}

function Get-NativePath {
    param([Parameter(Mandatory)][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'A required path is empty.' }
    if ($Path -split '[\\/]' | Where-Object { $_ -eq '..' }) {
        throw "A deployment path must not contain '..': $Path"
    }
    # Paths rooted only at the current drive (for example ``\\data``) and
    # drive-relative paths (``C:data``) are deliberately rejected.  They do
    # not have the stable, absolute meaning required for destructive actions.
    if ($Path -notmatch '^[A-Za-z]:[\\/]' -and $Path -notmatch '^\\\\[^\\/]+[\\/][^\\/]+') {
        throw "A deployment path must be fully qualified: $Path"
    }
    return [System.IO.Path]::GetFullPath($Path)
}

function Test-CaretakerFilesystemRoot {
    param([Parameter(Mandatory)][string]$Path)
    $root = [System.IO.Path]::GetPathRoot($Path)
    return [string]::Equals($Path.TrimEnd('\', '/'), $root.TrimEnd('\', '/'), [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-CaretakerPathBelow {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Parent)
    $prefix = $Parent.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    return [string]::Equals($Path, $Parent, [System.StringComparison]::OrdinalIgnoreCase) -or
        $Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-CaretakerPhysicalPath {
    param([Parameter(Mandatory)][string]$Path)
    Initialize-CaretakerNativeLock
    # Resolve the deepest existing component through a handle.  Windows path
    # parsing has already traversed every existing ancestor junction/reparse
    # point at that point; append only the as-yet non-existent suffix.
    $current = $Path
    $suffix = [System.Collections.Generic.List[string]]::new()
    while (-not (Test-Path -LiteralPath $current)) {
        $parent = Split-Path -Parent $current
        if (-not $parent -or [string]::Equals($parent, $current, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Could not resolve an existing ancestor for deployment path: $Path"
        }
        $suffix.Insert(0, (Split-Path -Leaf $current))
        $current = $parent
    }
    $fileReadAttributes = [uint32]0x80; $openExisting = [uint32]3
    $shareReadWriteDelete = [uint32]0x7; $backupSemantics = [uint32]0x02000000
    $handle = [Caretaker.NativeLock]::CreateFile($current, $fileReadAttributes, $shareReadWriteDelete,
        [IntPtr]::Zero, $openExisting, $backupSemantics, [IntPtr]::Zero)
    if ($handle.IsInvalid) { throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error()) }
    try {
        $physical = Get-CaretakerFinalPathByHandle $handle
    } finally {
        $handle.Dispose()
    }
    foreach ($part in $suffix) { $physical = $physical.TrimEnd('\', '/') + '\' + $part }
    return $physical
}

function Get-PalworldPaths {
    param([hashtable]$Config)
    $install = Get-NativePath (Get-ConfigValue $Config 'PALWORLD_INSTALL_ROOT')
    $serverValue = Get-ConfigValue $Config 'PALWORLD_SERVER_ROOT'
    $server = if ($serverValue) { Get-NativePath $serverValue } else { Get-NativePath (Join-Path $install 'server') }
    $backup = Get-NativePath (Get-ConfigValue $Config 'PALWORLD_BACKUP_DIR')
    if ((Test-CaretakerFilesystemRoot $install) -or (Test-CaretakerFilesystemRoot $server) -or (Test-CaretakerFilesystemRoot $backup)) {
        throw 'Deployment paths must not be a filesystem root.'
    }
    if ((Test-CaretakerPathBelow $backup $install) -or (Test-CaretakerPathBelow $install $backup) -or
        (Test-CaretakerPathBelow $backup $server) -or (Test-CaretakerPathBelow $server $backup)) {
        throw 'PALWORLD_BACKUP_DIR must not overlap the Palworld installation or server root.'
    }
    $physicalInstall = Get-CaretakerPhysicalPath $install
    $physicalServer = Get-CaretakerPhysicalPath $server
    $physicalBackup = Get-CaretakerPhysicalPath $backup
    if ((Test-CaretakerPathBelow $physicalBackup $physicalInstall) -or (Test-CaretakerPathBelow $physicalInstall $physicalBackup) -or
        (Test-CaretakerPathBelow $physicalBackup $physicalServer) -or (Test-CaretakerPathBelow $physicalServer $physicalBackup)) {
        throw 'PALWORLD_BACKUP_DIR must not overlap the Palworld installation or server root.'
    }
    return @{
        Install = $install; Server = $server
        Save = Join-Path $server 'Pal\Saved\SaveGames'
        Config = Join-Path $server 'Pal\Saved\Config'
        Backup = $backup
        LocalBackup = Join-Path $install 'backups-local'
    }
}

function Assert-RealDirectory {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw "$Label is missing: $Path" }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.LinkType -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "$Label must not be a symbolic link, junction, or reparse point: $Path"
    }
}

function Assert-RealFile {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "$Label is missing: $Path" }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.LinkType -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "$Label must not be a symbolic link or reparse point: $Path"
    }
}

function Assert-SafeTree {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Label)
    Assert-RealDirectory $Path $Label
    $link = Get-ChildItem -LiteralPath $Path -Force -Recurse |
        Where-Object { $_.LinkType -or ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) } |
        Select-Object -First 1
    if ($link) { throw "$Label contains a symbolic link: $($link.FullName)" }
}

function Get-CaretakerOperationLockPath {
    if ($env:PALWORLD_OPERATION_LOCK_FILE) { return Get-NativePath $env:PALWORLD_OPERATION_LOCK_FILE }
    $programData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    return Join-Path $programData 'Palworld\operation.lock'
}

function Initialize-CaretakerNativeLock {
    if (-not ('Caretaker.NativeLock' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace Caretaker {
    public static class NativeLock {
        public const int FileIdInfo = 18;

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern SafeFileHandle CreateFile(
            string name, uint access, uint share, IntPtr security,
            uint disposition, uint flags, IntPtr template);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool GetFileInformationByHandle(
            SafeFileHandle handle, out BY_HANDLE_FILE_INFORMATION information);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool GetFileInformationByHandleEx(
            SafeFileHandle handle, int fileInformationClass,
            out FILE_ID_INFO information, uint bufferSize);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern uint GetFinalPathNameByHandle(
            SafeFileHandle handle, System.Text.StringBuilder path,
            uint length, uint flags);

        [StructLayout(LayoutKind.Sequential)]
        public struct BY_HANDLE_FILE_INFORMATION {
            public uint FileAttributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
            public uint VolumeSerialNumber;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint NumberOfLinks;
            public uint FileIndexHigh;
            public uint FileIndexLow;
        }

        // FILE_ID_INFO / FILE_ID_128 from winbase.h.  FileIdInfo (18) returns
        // the 128-bit ID required for unique identities on ReFS as well as
        // NTFS; the legacy FileIndex fields above are only 64 bits.
        [StructLayout(LayoutKind.Sequential)]
        public struct FILE_ID_INFO {
            public ulong VolumeSerialNumber;
            [MarshalAs(UnmanagedType.ByValArray, SizeConst = 16)]
            public byte[] FileId;
        }
    }
}
'@
    }
    # A 32K buffer covers the maximum extended-length Windows path.  The
    # returned spelling is handle-derived (\\?\C:\...), never a lexical
    # re-normalization of an attacker-controlled pathname.
}

function Get-CaretakerFinalPathByHandle {
    param([Parameter(Mandatory)][Microsoft.Win32.SafeHandles.SafeFileHandle]$Handle)
    Initialize-CaretakerNativeLock
    $capacity = 32768
    $buffer = [System.Text.StringBuilder]::new($capacity)
    $length = [Caretaker.NativeLock]::GetFinalPathNameByHandle($Handle, $buffer, [uint32]$capacity, [uint32]0)
    if ($length -eq 0 -or $length -ge $capacity) {
        throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    return $buffer.ToString()
}

function Get-CaretakerFileIdentity {
    param([Parameter(Mandatory)][Microsoft.Win32.SafeHandles.SafeFileHandle]$Handle)
    Initialize-CaretakerNativeLock
    $information = New-Object Caretaker.NativeLock+FILE_ID_INFO
    # FileIdInfo is the documented FILE_INFO_BY_HANDLE_CLASS value for
    # FILE_ID_INFO.  Unlike FileIndexHigh/FileIndexLow, this 128-bit value is
    # unique on ReFS and other modern Windows file systems.
    $fileIdInfo = [Caretaker.NativeLock]::FileIdInfo
    $size = [uint32][Runtime.InteropServices.Marshal]::SizeOf([Caretaker.NativeLock+FILE_ID_INFO])
    if (-not [Caretaker.NativeLock]::GetFileInformationByHandleEx($Handle, $fileIdInfo, [ref]$information, $size)) {
        throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    if ($null -eq $information.FileId -or $information.FileId.Length -ne 16) {
        throw 'GetFileInformationByHandleEx did not return a 128-bit file identifier.'
    }
    $fileId = ($information.FileId | ForEach-Object { $_.ToString('X2') }) -join ''
    return ('{0:X16}:{1}' -f $information.VolumeSerialNumber, $fileId)
}

function Test-CaretakerDirectoryIdentityMatch {
    [OutputType([bool])]
    param(
        [Parameter(Mandatory)][string]$ExpectedIdentity,
        [Parameter(Mandatory)][string]$ActualIdentity
    )
    # File identities are an exact volume serial number plus 128-bit FileId.
    # Keep this comparison independent of path spelling so it can be tested
    # directly and so a same-name directory replacement cannot pass.
    return [string]::Equals($ExpectedIdentity, $ActualIdentity, [System.StringComparison]::Ordinal)
}

function Open-CaretakerOperationLockParent {
    param([Parameter(Mandatory)][string]$Path)
    Initialize-CaretakerNativeLock
    $fileReadAttributes = [uint32]0x80; $openExisting = [uint32]3
    $shareReadWrite = [uint32]0x3; $backupSemantics = [uint32]0x02000000; $openReparsePoint = [uint32]0x00200000
    # Deliberately omit FILE_SHARE_DELETE. Windows will now reject a rename or
    # deletion of this directory until the lock file has been opened and
    # validated beneath this exact parent handle.
    $handle = [Caretaker.NativeLock]::CreateFile($Path, $fileReadAttributes, $shareReadWrite,
        [IntPtr]::Zero, $openExisting, ($backupSemantics -bor $openReparsePoint), [IntPtr]::Zero)
    if ($handle.IsInvalid) { throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error()) }
    try {
        $information = New-Object Caretaker.NativeLock+BY_HANDLE_FILE_INFORMATION
        if (-not [Caretaker.NativeLock]::GetFileInformationByHandle($handle, [ref]$information)) {
            throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
        }
        if (-not ($information.FileAttributes -band [uint32]0x0010)) { throw 'Operation lock parent must be a directory.' }
        if ($information.FileAttributes -band [uint32]0x0400) { throw 'Operation lock parent must not be a reparse point.' }
        return [pscustomobject]@{
            Handle = $handle
            Identity = Get-CaretakerFileIdentity $handle
        }
    } catch {
        $handle.Dispose()
        throw
    }
}

function New-CaretakerSecureOperationLockStream {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$ExpectedPath)
    Initialize-CaretakerNativeLock
    $genericRead = [uint32]0x80000000; $genericWrite = [uint32]0x40000000
    $openAlways = [uint32]4; $fileAttributeNormal = [uint32]0x80; $openReparsePoint = [uint32]0x00200000
    # No sharing prevents a pathname replacement/delete after the secure
    # handle is acquired.  The byte-range lock remains interoperable with
    # Python for callers that already hold the shared operation lock.
    $handle = [Caretaker.NativeLock]::CreateFile($Path, ($genericRead -bor $genericWrite), [uint32]0,
        [IntPtr]::Zero, $openAlways, ($fileAttributeNormal -bor $openReparsePoint), [IntPtr]::Zero)
    if ($handle.IsInvalid) { throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error()) }
    try {
        $information = New-Object Caretaker.NativeLock+BY_HANDLE_FILE_INFORMATION
        if (-not [Caretaker.NativeLock]::GetFileInformationByHandle($handle, [ref]$information)) {
            throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
        }
        if ($information.FileAttributes -band [uint32]0x0400) { throw 'Operation lock must not be a reparse point.' }
        if ($information.FileAttributes -band [uint32]0x0010) { throw 'Operation lock must be a regular file.' }
        $actualPath = Get-CaretakerFinalPathByHandle $handle
        if (-not [string]::Equals($actualPath, $ExpectedPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'Operation lock resolved outside its expected parent directory.'
        }
        # The stream owns this already-validated handle.  Do not validate the
        # pathname again: it could now name a different object.
        return [System.IO.FileStream]::new($handle, [System.IO.FileAccess]::ReadWrite, 1, $false)
    } catch {
        $handle.Dispose()
        throw
    }
}

function Enter-CaretakerOperationLock {
    $path = Get-CaretakerOperationLockPath
    $parent = Split-Path -Parent $path
    if (-not (Test-Path -LiteralPath $parent)) { [System.IO.Directory]::CreateDirectory($parent) | Out-Null }
    Assert-RealDirectory $parent 'Operation lock directory'
    $stream = $null
    $parentLock = $null
    $verifiedParentLock = $null
    try {
        # Hold the real parent without FILE_SHARE_DELETE throughout the child
        # CreateFile and final-path validation. This closes the same-spelling
        # directory rename-swap race that a pathname comparison alone cannot.
        $parentLock = Open-CaretakerOperationLockParent $parent
        $expectedParentPath = (Get-CaretakerFinalPathByHandle $parentLock.Handle).TrimEnd('\', '/')
        $expectedPath = $expectedParentPath + '\' + (Split-Path -Leaf $path)
        $stream = New-CaretakerSecureOperationLockStream $path $expectedPath
        # Re-open the pathname and compare its object identity with the parent
        # that was held before CreateFile. This second defense is not based on
        # a path spelling, so a same-name replacement directory cannot pass.
        $verifiedParentLock = Open-CaretakerOperationLockParent $parent
        if (-not (Test-CaretakerDirectoryIdentityMatch -ExpectedIdentity $parentLock.Identity -ActualIdentity $verifiedParentLock.Identity)) {
            throw 'Operation lock parent directory changed during acquisition.'
        }
        if (-not [string]::Equals($expectedParentPath, (Get-CaretakerFinalPathByHandle $verifiedParentLock.Handle).TrimEnd('\', '/'), [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'Operation lock parent directory changed during acquisition.'
        }
        # FileStream.Lock is a Win32 byte-range lock, interoperable with
        # Python's msvcrt.locking on the same one-byte lock file.
        $stream.Lock(0, 1)
        $stream | Add-Member -NotePropertyName CaretakerLockHeld -NotePropertyValue $true
        # Keep the no-delete parent handle for the operation lifetime, not
        # merely acquisition, so the published lock cannot later be detached
        # from its pathname by a directory rename.
        $stream | Add-Member -NotePropertyName CaretakerLockParentHandle -NotePropertyValue $parentLock.Handle
        $parentLock = $null
        return $stream
    } catch {
        if ($stream) { $stream.Dispose() }
        throw 'Another Palworld operation is active, or the operation lock is unsafe.'
    } finally {
        if ($verifiedParentLock) { $verifiedParentLock.Handle.Dispose() }
        if ($parentLock) { $parentLock.Handle.Dispose() }
    }
}

function Exit-CaretakerOperationLock {
    param($Lock)
    if ($null -eq $Lock) { return }
    try {
        if ($Lock.PSObject.Properties['CaretakerLockHeld'] -and $Lock.CaretakerLockHeld) {
            try { $Lock.Unlock(0, 1) } finally { $Lock.CaretakerLockHeld = $false }
        }
    } finally {
        try {
            if ($Lock.PSObject.Properties['CaretakerLockParentHandle']) {
                $Lock.CaretakerLockParentHandle.Dispose()
            }
        } finally { $Lock.Dispose() }
    }
}

function Test-SnapshotName { param([string]$Name) return $Name -match '^palworld-\d{8}-\d{6}$' }

function Get-SnapshotManifest {
    param([Parameter(Mandatory)][string]$Snapshot)
    $files = @{}
    Get-ChildItem -LiteralPath $Snapshot -File -Force -Recurse | ForEach-Object {
        $relative = $_.FullName.Substring($Snapshot.Length).TrimStart('\', '/') -replace '\\', '/'
        # The manifest describes snapshot payload files, never itself.
        if ($relative -ne 'metadata/manifest.json') {
            $files[$relative] = @{ size = [int64]$_.Length; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
        }
    }
    return @{ format = 2; files = $files }
}

function Assert-SnapshotManifest {
    param([Parameter(Mandatory)][string]$Snapshot)
    $manifestPath = Join-Path $Snapshot 'metadata\manifest.json'
    Assert-RealFile $manifestPath 'Backup manifest'
    try { $manifest = [System.IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'Backup manifest is invalid JSON.' }
    if ($null -eq $manifest -or $manifest.format -ne 2 -or $null -eq $manifest.files) {
        throw 'Backup manifest has an invalid structure.'
    }
    $actual = Get-SnapshotManifest $Snapshot
    $properties = @($manifest.files.PSObject.Properties)
    if ($properties.Count -ne $actual.files.Count) { throw 'Backup manifest does not match snapshot contents.' }
    $verified = @{}
    foreach ($property in $properties) {
        $name = [string]$property.Name
        if ($name -notmatch '^(?:savegames|config)/[^\\/]+(?:/[^\\/]+)*$' -or -not $actual.files.ContainsKey($name)) {
            throw 'Backup manifest does not match snapshot contents.'
        }
        $entry = $property.Value
        if ($null -eq $entry -or $null -eq $entry.size -or $null -eq $entry.sha256) {
            throw 'Backup manifest has an invalid file entry.'
        }
        $size = $entry.size; $hash = [string]$entry.sha256
        if (($size -isnot [int64] -and $size -isnot [int32] -and $size -isnot [double]) -or
            [double]$size -lt 0 -or [double]$size -ne [math]::Truncate([double]$size) -or
            $hash -notmatch '^[a-fA-F0-9]{64}$' -or
            [int64]$size -ne [int64]$actual.files[$name].size -or
            -not [string]::Equals($hash, [string]$actual.files[$name].sha256, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'Backup manifest does not match snapshot contents.'
        }
        $verified[$name] = @{ size = [int64]$size; sha256 = $hash.ToLowerInvariant() }
    }
    return @{ format = 2; files = $verified }
}

Export-ModuleMember -Function Get-CaretakerConfig, Get-ConfigValue, Get-NativePath, Get-PalworldPaths, Assert-RealDirectory, Assert-RealFile, Assert-SafeTree, Get-CaretakerOperationLockPath, Enter-CaretakerOperationLock, Exit-CaretakerOperationLock, Test-SnapshotName, Get-SnapshotManifest, Assert-SnapshotManifest
