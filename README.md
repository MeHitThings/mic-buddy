# Mic Buddy

A cute little overlay that sits on your screen and shows you if your mic is on or off in OBS!

**Pink happy face** = Your mic is LIVE! People can hear you!

**Purple straight face** = Your mic is muted. Nobody can hear you.

## Download

1. Go to the [Releases](https://github.com/MeHitThings/mic-buddy/releases) page
2. Download **Mic Buddy.exe**
3. That's it! No install needed

## One-Time OBS Setup

Mic Buddy talks to OBS using something called WebSocket. You just need to turn it on once:

1. Open **OBS Studio**
2. Go to **Tools** > **WebSocket Server Settings**
3. Check **Enable WebSocket Server**
4. Make sure the port is **4455**
5. Make sure **Enable Authentication** is **unchecked** (no password needed)
6. Click **OK**

Done! You only have to do this once.

## How to Use

1. Open **OBS** (Mic Buddy will wait for it if it's not open yet)
2. Run **Mic Buddy.exe**
3. A little face appears in the corner of your screen!

That's all! Mic Buddy finds OBS on its own and starts watching your mic.

- **Drag** the face anywhere you want on your screen
- **Right-click** the icon in the system tray to reset position or quit
- It remembers where you put it!

## What the Faces Mean

| Face | Color | Meaning |
|------|-------|---------|
| Happy smiley | Pink | Your mic is ON - you're live! |
| Straight face | Purple | Your mic is MUTED - you're safe! |

The face smoothly changes between the two so you always know what's going on.
