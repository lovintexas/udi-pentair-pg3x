# Pentair PG3x Plugin

A Universal Devices eisy / PG3x plugin for supported Pentair connected pool equipment.

## Documentation

Full documentation and setup guide:

https://lovintexas.github.io/udi-pentair-pg3x/

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

### IntelliCenter
- Automatic discovery of IntelliCenter equipment
- Pool and spa body status and temperature
- Pool and spa On / Off control
- Heat setpoint control
- Heat source selection
- IntelliCenter pump status and control
- Automatic discovery of pump circuit assignments
- RPM / GPM mode selection and rate control for pump assignments
- Heater status and control
- IntelliChem pH, ORP, salt, alkalinity, calcium, and CYA monitoring
- IntelliChem pH and ORP setpoint control
- IntelliChlor salt level, pool/spa output, and Super Chlorinate status
- IntelliChlor pool and spa output control (0–100%, 5% increments)
- IntelliChlor Super Chlorinate Start / Stop control
- Auxiliary/circuit On / Off control
- IntelliCenter IntelliBrite and GloBrite color-light On / Off control
- Color and light-show selection
- Hold and Recall light commands

## Configuration

Configure the following PG3x Custom Parameters:

- `username` — Pentair account email address
- `password` — Pentair account password

The plugin authenticates to the Pentair cloud and automatically discovers supported equipment associated with the account.

## Notes

Other Pentair device types may be discovered and logged but are ignored until explicit support is added.

This project is not affiliated with or endorsed by Pentair.
