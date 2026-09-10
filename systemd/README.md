# Units

Installed to `~/.config/systemd/user/` by `tools/install-units.sh`. User units,
not system units — everything Herald touches belongs to the user's account, and
`sudo loginctl enable-linger $USER` keeps them running when nobody is logged in.

    herald-collect.service / .timer      ingestion, every 30 min
    herald-cycle@.service                templated; one instance per cycle
    herald-cycle-dawn.timer              06:30
    herald-cycle-scout.timer             08:15 and 16:45
    herald-brain.service                 the persistent Remote Control session
    herald-telegram.service              the Telegram bridge
    herald-health-webhook.service        inbound Apple Health data
    herald-watchdog.service / .timer     health probes, every 5 min

`Persistent=true` only on `dawn`: a missed morning digest is worth catching up
on when the box comes back, a missed midday sweep is not.

Check on things with:

    systemctl --user list-timers 'herald*'
    journalctl --user -u herald-cycle@dawn -n 100

`herald-watchdog` replaced `herald-brain-watchdog` on 8 Sep 2026. The old one
only restarted the brain, so when herald-telegram wedged on an fd leak it stayed
`active (running)` and silently deaf for two hours. The new one probes fd
pressure and a real heartbeat as well, and covers every long-lived unit --
see the module docstring in `bin/herald-watchdog`.
