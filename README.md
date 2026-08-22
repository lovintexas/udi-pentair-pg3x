# Pentair PG3x Plugin

A Universal Devices eisy / PG3x plugin for supported Pentair connected pool equipment.

## Currently Supported

### IntelliFlo3 / IntelliFlo Pro3 VSF
- Online / connection status
- Power
- Motor speed
- Flow
- Pressure
- Alarm status
- Active program
- Automatic discovery of configured pump programs
- Start / Stop pump programs
- Immediate Running / Idle feedback after commands

### Color Sync
- Online / connection status
- On / Off
- Current color or light show
- Red
- White
- Magenta
- Green
- Blue
- Hold
- Recall
- SAm
- Party
- Romance
- Caribbean
- American
- Sunset
- Royal

Color Sync commands update IoX immediately after the Pentair command is accepted. Normal polling later confirms the actual device state.

## Configuration

Configure the following PG3x Custom Parameters:

- `username` — Pentair account email address
- `password` — Pentair account password

The plugin authenticates to the Pentair cloud and automatically discovers supported equipment associated with the account.

## Notes

Other Pentair device types may be discovered and logged but are ignored until explicit support is added.

This project is not affiliated with or endorsed by Pentair.
