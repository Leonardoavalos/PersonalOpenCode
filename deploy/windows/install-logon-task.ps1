# Registers a logon task that boots the WSL distro and keeps its VM alive, so
# the systemd user service starts without anyone opening a terminal.
#
# Runs as the current user; no elevation required. If this fails with
# "Acceso denegado", use an elevated PowerShell.
#
# Why an anchor process instead of a plain `wsl.exe --exec /bin/true`:
# Windows shuts an idle WSL VM down, which would take the service with it. A
# long-lived exec keeps a client attached to the VM for as long as the user is
# logged on.
#
# Remove with:
#   Unregister-ScheduledTask -TaskName 'OpenCodeTelegram' -Confirm:$false

$ErrorActionPreference = 'Stop'

$distro = if ($env:OPENCODE_WSL_DISTRO) { $env:OPENCODE_WSL_DISTRO } else { 'Ubuntu' }

$action = New-ScheduledTaskAction -Execute 'C:\WINDOWS\System32\wsl.exe' `
  -Argument "-d $distro --exec /bin/sh -c `"exec sleep infinity`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
  -TaskName 'OpenCodeTelegram' `
  -Action $action `
  -Trigger $trigger `
  -Settings $settings `
  -Description "Boots $distro and keeps the WSL VM alive so the OpenCode Telegram bridge service starts" `
  -Force | Out-Null

Write-Output "Registered task OpenCodeTelegram for user $env:USERNAME, distro $distro"
