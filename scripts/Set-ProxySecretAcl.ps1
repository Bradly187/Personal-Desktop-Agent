param(
    [Parameter(Mandatory = $true)]
    [string]$Path,

    [switch]$Directory
)

$ErrorActionPreference = 'Stop'

$resolved = (Resolve-Path -LiteralPath $Path).Path
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
Set-Acl -LiteralPath $resolved -AclObject $acl

# Fail closed unless the effective DACL is protected and every remaining ACE
# belongs to the current SID. Never print the ACL or credential path contents.
$actual = Get-Acl -LiteralPath $resolved
$ownerSid = $actual.Owner | ForEach-Object {
    (New-Object System.Security.Principal.NTAccount($_)).Translate(
        [System.Security.Principal.SecurityIdentifier]
    )
}
if (-not $actual.AreAccessRulesProtected -or $ownerSid.Value -ne $currentSid.Value) {
    throw 'Proxy secret ACL ownership/protection verification failed.'
}
foreach ($entry in $actual.Access) {
    $entrySid = $entry.IdentityReference.Translate(
        [System.Security.Principal.SecurityIdentifier]
    )
    if ($entrySid.Value -ne $currentSid.Value -or
        $entry.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {
        throw 'Proxy secret ACL contains an unexpected principal or deny rule.'
    }
}
if (@($actual.Access).Count -eq 0) {
    throw 'Proxy secret ACL contains no current-user grant.'
}
