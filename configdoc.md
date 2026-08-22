# Configure the Pentair Cloud Plugin

This plugin connects to the same Pentair Cloud account used by the Pentair Pool phone app.

## Configuration

Enter your Pentair Pool account credentials in the Custom Parameters:

`username`

Your Pentair account email address.

`password`

Your Pentair account password.

After entering both values, click **Save**.

The plugin will authenticate with Pentair Cloud and automatically discover supported equipment associated with the account.

## Currently Supported Devices

### IntelliFlo3 / IntelliFlo Pro3 VSF

Pentair device type `IF31`.

The plugin reports:

- Online status
- Current power
- Motor speed
- Flow
- Pressure
- Alarm code
- Active program
- Configured pump programs

Each enabled pump program is discovered automatically and appears as its own IoX node.

### Color Sync

Pentair device type `PLC1`.

The Color Sync controller is automatically discovered and appears as an IoX node.

The plugin supports Color Sync power control and selection of supported colors and light shows.

## Other Pentair Equipment

Pentair accounts may contain other connected device types.

Unknown devices are logged but are not added to IoX until support for that device type has been implemented and tested.

## Security

Your Pentair username and password are used only to authenticate with Pentair Cloud.

Do not include your password, authentication tokens, or device identifiers in screenshots, forum posts, logs, or support requests.
