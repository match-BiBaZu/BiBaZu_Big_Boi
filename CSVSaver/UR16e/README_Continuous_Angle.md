# Continuous UR angle program

`BiBaZu_Continuous.urp` accepts every integer angle from `155` through `210`
on RTDE input register 42. The integer is interpreted as tenths of a degree.

The target orientation in base RPY coordinates is:

- Roll: -45 degrees
- Pitch / Ry: requested angle, 15.5 through 21.0 degrees
- Yaw: -90 degrees

## Re-teaching the rotation position

1. Open `BiBaZu_Continuous.urp` on the teach pendant.
2. Open **BeforeStart**.
3. Select the waypoint **Rotation_Position**.
4. Move the TCP to the new rotation position and press **Set waypoint**.
5. Save the program.

At program start the robot moves to this waypoint once and captures its current
TCP position. Subsequent angle commands preserve that captured XYZ position.
Start the program and leave it running before pressing **Apply UR Angle** in the
Pressure Control GUI.

The external RTDE client uses input integer registers 42/43. No second RTDE
client may control these two registers at the same time.
