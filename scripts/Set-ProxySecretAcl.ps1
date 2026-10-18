param(
    [Parameter(Mandatory = $true)]
    [string]$Path,

    [switch]$Directory
)

$ErrorActionPreference = 'Stop'

# Use .NET file-security APIs rather than Resolve-Path/Set-Acl/Get-Acl:
# Windows PowerShell 5.1 started from a PowerShell 7 session inherits a
# PSModulePath under which Microsoft.PowerShell.Security fails to autoload.
$resolved = [System.IO.Path]::GetFullPath($Path)
if ($Directory) {
    if (-not [System.IO.Directory]::Exists($resolved)) {
        throw 'Proxy secret directory does not exist.'
    }
} elseif (-not [System.IO.File]::Exists($resolved)) {
    throw 'Proxy secret file does not exist.'
}
$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User

if ($Directory) {
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $inheritance = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $currentSid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        $inheritance,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
} else {
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $currentSid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
}

# Build a new protected DACL rather than editing the old one. This removes both
# inherited and pre-existing explicit grants to every other principal.
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner($currentSid)
[void]$acl.AddAccessRule($rule)
if ($Directory) {
    [System.IO.Directory]::SetAccessControl($resolved, $acl)
} else {
    [System.IO.File]::SetAccessControl($resolved, $acl)
}

# Fail closed unless the effective DACL is protected and every remaining ACE
# belongs to the current SID. Never print the ACL or credential path contents.
# Read owner and ACEs as SIDs directly: translating a host-qualified account
# name back to a SID can fail on hosts whose account name does not resolve.
$sidType = [System.Security.Principal.SecurityIdentifier]
$actual = if ($Directory) {
    [System.IO.Directory]::GetAccessControl($resolved)
} else {
    [System.IO.File]::GetAccessControl($resolved)
}
$ownerSid = $actual.GetOwner($sidType)
if (-not $actual.AreAccessRulesProtected -or $null -eq $ownerSid -or
    $ownerSid.Value -ne $currentSid.Value) {
    throw 'Proxy secret ACL ownership/protection verification failed.'
}
$entries = @($actual.GetAccessRules($true, $true, $sidType))
foreach ($entry in $entries) {
    if ($entry.IdentityReference.Value -ne $currentSid.Value -or
        $entry.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {
        throw 'Proxy secret ACL contains an unexpected principal or deny rule.'
    }
}
if ($entries.Count -eq 0) {
    throw 'Proxy secret ACL contains no current-user grant.'
}
