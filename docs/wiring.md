# Solar Tracker Wiring and Bill of Materials

## Schematic

```text
                           ARDUINO UNO / NANO
                    +--------------------------------+
                    |                                |
     +5 V ----------+--[ LDR LEFT ]--+               |
                                      +---- A0        |
     GND -----------+--[ 10 kΩ ]------+               |
                                                     |
     +5 V ----------+--[ LDR RIGHT ]-+               |
                                     +----- A1        |
     GND -----------+--[ 10 kΩ ]-----+               |
                                                     |
     D9 -------------------------------- SERVO SIG    |
     GND ------------------------------ SERVO GND     |
     External regulated +5 V ---------- SERVO +5 V   |
     External GND ---------------------+              |
                                        \             |
     Arduino GND ------------------------+ common     |
     D13 (on-board LED) ---------------- fault LED    |
                    +--------------------------------+
```

Each LDR and 10 kΩ resistor form a voltage divider. The divider midpoint goes to
the matching analog input. The resistor shown is connected from the analog input
to GND; connect the LDR from +5 V to the analog input.

Power the servo from a regulated supply suitable for its stall current rather
than from the Arduino 5 V pin. Join the servo-supply ground to Arduino GND.
Without a common ground, the servo signal may be unreliable. The sketch uses the
Arduino's built-in D13 LED as its default fault indicator; an external LED is
optional and must include a series resistor (typically 220–330 Ω).

## Bill of Materials

| Qty | Part | Notes |
|---:|---|---|
| 1 | Arduino Uno or Nano | ATmega328P-compatible AVR board |
| 2 | Light-dependent resistors | Matched parts are preferable |
| 2 | 10 kΩ resistors | LDR voltage-divider resistors |
| 1 | SG90 or compatible positional servo | Connect signal to D9 |
| 1 | Regulated 5 V servo supply | Rated for the servo stall current |
| 1 | Breadboard and jumper wires | For the sensor dividers and signal connections |
| 1 | 220–330 Ω resistor and LED (optional) | Only if adding an external fault LED |

Disconnect power before changing wiring. Verify polarity and the servo supply's
current rating before powering the circuit.
