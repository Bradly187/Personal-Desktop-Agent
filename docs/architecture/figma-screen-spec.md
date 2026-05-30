# DesktopAgent iPad UI — Figma Screen Spec

Canvas: **iPad Pro 12.9" Landscape — 1366 × 1024 pt**  
Partial Figma file (tokens + text styles already set): https://www.figma.com/design/F4ECU6VbRLxWyWybAxu5U7

---

## Design Tokens

### Colors

| Token | Hex | Usage |
|-------|-----|-------|
| `surface/primary` | `#F7F7F7` | Page background |
| `surface/secondary` | `#ECECF1` | Button/key backgrounds |
| `surface/tertiary` | `#E0E0E8` | Pressed/active states |
| `accent` | `#3478F6` | Primary actions, Send button, tab indicator |
| `text/primary` | `#121215` | All body text, button labels |
| `text/secondary` | `#6E6E78` | Hints, secondary labels, slider values |
| `status/connected` | `#32C75A` | Connection banner — connected |
| `status/connecting` | `#FFBF00` | Connection banner — connecting |
| `status/disconnected` | `#FF3B30` | Connection banner — disconnected |
| `key/number` | `#FFFFFF` | Keypad number key background |
| `key/operator` | `#EFEFF E` | Keypad operator key background |
| `key/function` | `#E7EFFF` | Keypad function key background |
| `key/send` | `#3478F6` | Keypad Send key (= accent) |
| `white` | `#FFFFFF` | Banner text, Send key label |
| `destructive` | `#E84235` | Clear/trash button |
| `trackpad/surface` | `#F4F4F6` | Trackpad area fill |
| `canvas/background` | `#FFFFFF` | Handwriting canvas |

### Typography

| Style | Font | Size | Weight | Line Height |
|-------|------|------|--------|-------------|
| `display-mono/32` | Roboto Mono | 32pt | Light | 120% |
| `heading/20` | Inter | 20pt | Medium | 120% |
| `button-label/15` | Inter | 15pt | Medium | 120% |
| `body/15` | Inter | 15pt | Regular | 140% |
| `setting-label/17` | Inter | 17pt | Regular | 130% |
| `key-label/18` | Inter | 18pt | Medium | 120% |
| `command-label/12` | Inter | 12pt | Regular | 130% |
| `caption/11` | Inter | 11pt | Regular | 130% |

### Spacing Scale

`4 / 8 / 12 / 16 / 20 / 24 pt`  
Corner radii: `8 / 10 / 12 pt`

---

## Shared Chrome (applied to every screen)

### Connection Status Banner
```
Frame: StatusBanner
  w=1366, h=44, x=0, y=0
  layoutMode=HORIZONTAL, primaryAxisAlignItems=CENTER, counterAxisAlignItems=CENTER
  paddingLeft=16, paddingRight=16, itemSpacing=8
  fill=status/* (varies by state)

  Text "● Connected · 192.168.1.100:8765"
    style=button-label/15, fill=white
```
**Three states:** swap fill color only — layout identical.
- Connected: fill=`status/connected` (#32C75A), label "● Connected · 192.168.1.100:8765"
- Connecting: fill=`status/connecting` (#FFBF00), label "◌ Connecting…"
- Disconnected: fill=`status/disconnected` (#FF3B30), label "✕ Disconnected — tap to retry"

### Tab Bar
```
Frame: TabBar
  w=1366, h=83, x=0, y=941  (bottom of 1024pt canvas)
  layoutMode=HORIZONTAL, primaryAxisAlignItems=SPACE_BETWEEN
  counterAxisAlignItems=CENTER
  paddingLeft=0, paddingRight=0, paddingTop=0, paddingBottom=28
  fill=surface/primary
  stroke=surface/tertiary (top edge, 0.5pt)

  5× TabItem (w=273, h=83, layoutMode=VERTICAL, center-center, itemSpacing=4, paddingTop=8)
    Icon: 24×24, Unicode glyph
    Label: caption/11, fill=text/secondary (inactive) or accent (active)
    Indicator: w=32, h=3, cornerRadius=1.5, fill=accent (active only, hidden when inactive)
```

| Tab | Icon | Label |
|-----|------|-------|
| 1 | ⊞ | Commands |
| 2 | ✋ | Trackpad |
| 3 | ƒ | Keypad |
| 4 | ✏️ | Write |
| 5 | ⚙ | Settings |

### Content Area
All screens occupy the rect: `x=0, y=44, w=1366, h=897` (between banner and tab bar).

---

## Screen 1 — Trackpad (Tab 2 active)

```
Frame: Screen/Trackpad  1366×1024

  StatusBanner [Connected state]

  Frame: Content  x=0, y=44, w=1366, h=897
    fill=surface/primary

    Frame: TrackpadSurface
      x=12, y=8
      w=1342, h=741
      cornerRadius=12
      fill=trackpad/surface  (#F4F4F6)
      stroke=surface/tertiary, strokeWeight=1

      [Interior annotation]:
      Text: "Drag to move cursor" (center, text/secondary, body/15)
      Text: "Two-finger drag to scroll" (center+32pt below, caption/11, text/secondary)

    Frame: ClickButtonsRow
      x=12, y=761
      w=1342, h=56
      layoutMode=HORIZONTAL, itemSpacing=12
      cornerRadius=10

      Button: "Left Click"  w=fill, h=56, cornerRadius=10
        fill=surface/secondary
        label: button-label/15 "Left Click", fill=text/primary
        stroke=surface/tertiary, 1pt

      Button: "Right Click"  w=fill, h=56, cornerRadius=10
        fill=surface/secondary
        label: button-label/15 "Right Click", fill=text/primary

      Button: "⛶ Fullscreen"  w=180, h=56, cornerRadius=10
        fill=accent
        label: button-label/15 "Fullscreen", fill=white

    Frame: ShortcutButtonsRow
      x=12, y=829
      w=1342, h=48
      layoutMode=HORIZONTAL, itemSpacing=12

      5 buttons (w=fill, h=48, cornerRadius=10, fill=surface/tertiary):
        "⌘C Copy"  "⌘V Paste"  "⌘Z Undo"  "↓ Scroll ↓"  "↑ Scroll ↑"
        Each: VStack(icon 16pt + label caption/11), center-center

  TabBar [Tab 2 "Trackpad" active]
```

### Fullscreen Variant (separate artboard)
```
Frame: Screen/Trackpad-Fullscreen  1366×1024
  fill=trackpad/surface

  Frame: TrackpadSurface (fills entire screen, x=0, y=0, w=1366, h=1024)
    cornerRadius=0
    fill=trackpad/surface

  Button: CollapseOverlay  x=16, y=16, w=44, h=44, cornerRadius=22
    fill=surface/secondary (with 80% opacity)
    stroke=surface/tertiary
    label: "↙" (20pt, text/primary, center)

  [No tab bar, no status banner, no other controls]
```

---

## Screen 2 — Command Pad (Tab 1 active)

```
Frame: Screen/CommandPad  1366×1024

  StatusBanner [Connected state]

  Frame: Content  x=0, y=44, w=1366, h=897
    fill=surface/primary
    paddingLeft=12, paddingRight=12, paddingTop=12, paddingBottom=12

    Frame: NavBar  x=0, y=0, w=1342, h=44
      layoutMode=HORIZONTAL, counterAxisAlignItems=CENTER
      primaryAxisAlignItems=SPACE_BETWEEN

      Text: heading/20 "Commands", fill=text/primary
      Button: "Edit"  body/15, fill=accent

    Frame: CommandGrid  x=0, y=56, w=1342, h=829
      layoutMode=HORIZONTAL (wrapping)  [simulate LazyVGrid adaptive ≥80pt]
      itemSpacing=12
      paddingAll=0

      12× CommandButton  (see component spec below):
        Row 1: Click · Right Click · Scroll ↓ · Scroll ↑ · Copy · Paste · Undo
        Row 2: Screenshot · Tab · Enter · Escape · Space
```

### CommandButton Component
```
Frame: CommandButton  80×80
  cornerRadius=12
  fill=surface/secondary
  layoutMode=VERTICAL, primaryAxisAlignItems=CENTER, counterAxisAlignItems=CENTER
  itemSpacing=4

  Icon: 22pt Unicode glyph, fill=text/primary
  Label: command-label/12, fill=text/primary, maxLines=2, center-aligned

  State: Pressed
    fill=accent (0.15 opacity overlay)
    Icon + Label: fill=accent
```

| # | Icon | Label |
|---|------|-------|
| 1 | 🖱 | Click |
| 2 | ⌥🖱 | Right Click |
| 3 | ↓ | Scroll ↓ |
| 4 | ↑ | Scroll ↑ |
| 5 | ⌘C | Copy |
| 6 | ⌘V | Paste |
| 7 | ⌘Z | Undo |
| 8 | 📷 | Screenshot |
| 9 | ⇥ | Tab |
| 10 | ↵ | Enter |
| 11 | ⎋ | Escape |
| 12 | ␣ | Space |

### Edit Mode Overlay
```
Frame: EditModeSheet (modal, bottom sheet)
  x=0, y=424  w=1366, h=600
  fill=surface/primary
  cornerRadius=top-left=16, top-right=16
  shadow: 0 -4px 24px rgba(0,0,0,0.12)

  NavBar: "Edit Commands" (heading/20) | "Reset" (destructive/15) | "Done" (accent/15)

  List: vertical scroll
    each row: h=56, layoutMode=HORIZONTAL, itemSpacing=12, paddingH=16
      Drag handle: "⠿" (text/secondary)
      Icon + Label (as CommandButton)
      Delete: "✕" circle (destructive)
```

---

## Screen 3 — Scientific Keypad (Tab 3 active)

```
Frame: Screen/Keypad  1366×1024

  StatusBanner [Connected state]

  Frame: Content  x=0, y=44, w=1366, h=897
    fill=surface/primary
    paddingLeft=8, paddingRight=8, paddingTop=8, paddingBottom=8

    ── Display Panel ──
    Frame: DisplayPanel  x=0, y=0, w=1350, h=96
      fill=surface/secondary, cornerRadius=12
      paddingLeft=16, paddingRight=16, paddingTop=12, paddingBottom=12
      layoutMode=VERTICAL, itemSpacing=4

      Text: ExpressionText  display-mono/32, fill=text/primary
        textAlignHorizontal=RIGHT
        characters="sin(π) + 2^8"

      Text: PreviewText  body/15, fill=text/secondary
        textAlignHorizontal=RIGHT
        characters="= 256"

    ── Mode Toggle ──
    Frame: ModeToggle  x=0, y=108, w=320, h=36
      layoutMode=HORIZONTAL, cornerRadius=8
      fill=surface/secondary, paddingAll=2, itemSpacing=2

      Pill[active]: "Basic"   w=156, h=32, cornerRadius=6, fill=white
        label: button-label/15, fill=text/primary
      Pill[inactive]: "Scientific"  w=156, h=32, cornerRadius=6, fill=transparent
        label: button-label/15, fill=text/secondary

    ── Key Grid: BASIC MODE ──
    Frame: KeyGrid  x=0, y=156, w=1350, h=733
      layoutMode=VERTICAL, itemSpacing=6

      5 rows × 4 keys (except row 5 = Send spans full width)
      Each key: w=(1350-18)/4≈333, h=134, cornerRadius=10

      Row 1: [AC] [DEL] [( )] [÷]
      Row 2: [7] [8] [9] [×]
      Row 3: [4] [5] [6] [−]
      Row 4: [1] [2] [3] [+]
      Row 5: [0] [.] [ANS] [Send ▶]

    ── Key Grid: SCIENTIFIC MODE ──
    (Replaces basic mode toggle — same frame position, different content)
    8 rows × 5 keys, each key: w=(1350-24)/5≈265, h=80

      Row 1: [sin] [cos] [tan] [log] [ln]
      Row 2: [sin⁻¹] [cos⁻¹] [tan⁻¹] [log₂] [√]
      Row 3: [π] [e] [^] [!] [|x|]
      Row 4: [±] [EE] [mod] [( )] [DEL]
      Row 5: [7] [8] [9] [÷] [×]
      Row 6: [4] [5] [6] [−] [+]
      Row 7: [1] [2] [3] [0] [.]
      Row 8: [ANS] spans 2, [Send ▶] spans 3
```

### CalcKey Component — 4 Variants

```
Base CalcKey: 64×64 (small) or 333×134 (basic grid cell)
  cornerRadius=10
  layoutMode=VERTICAL or HORIZONTAL, center-center

Variant: Number
  fill=key/number (#FFFFFF)
  stroke=surface/tertiary, 1pt
  label: key-label/18, fill=text/primary

Variant: Operator
  fill=key/operator (#EFEFFE)
  label: key-label/18, fill=accent

Variant: Function
  fill=key/function (#E7EFFF)
  label: key-label/18, fill=text/primary

Variant: Send
  fill=key/send (#3478F6) = accent
  label: button-label/15 "Send ▶", fill=white

Variant: Destructive (DEL, AC)
  fill=surface/secondary
  label: key-label/18, fill=destructive

State: Pressed
  fill=surface/tertiary (for Number/Operator/Function)
  scale=0.96 (spring animation in SwiftUI — annotate as motion note)
```

---

## Screen 4 — Handwriting Canvas (Tab 4 active)

```
Frame: Screen/Handwriting  1366×1024

  StatusBanner [Connected state]

  Frame: Content  x=0, y=44, w=1366, h=897
    fill=canvas/background (#FFFFFF)

    ── Canvas Area ──
    Frame: Canvas  x=0, y=0, w=1366, h=720
      fill=canvas/background
      [Annotation]: "PKCanvasView — Apple Pencil draws; finger touches pan"
      [Faint grid lines optional]: 1pt, surface/tertiary, every 32pt

    ── Floating Controls (bottom-right of canvas) ──
    Frame: FloatingControls  x=1222, y=648
      w=132, h=60
      layoutMode=HORIZONTAL, itemSpacing=8
      paddingLeft=12, paddingRight=12, paddingTop=12, paddingBottom=12
      fill=surface/primary (80% opacity blur → "regularMaterial")
      cornerRadius=12
      shadow: 0 2px 12px rgba(0,0,0,0.10)

      IconBtn: "↩" (undo)  w=32, h=32, fill=transparent, label 20pt text/primary
      IconBtn: "🗑" (clear) w=32, h=32, fill=transparent, label 20pt destructive
      Btn: "✨ Recognise"  w=fill, h=32, cornerRadius=8
        fill=surface/secondary
        stroke=accent, 1pt
        label: button-label/15 "Recognise", fill=accent

    ── Result Bar (appears after recognition) ──
    Frame: ResultBar  x=0, y=724, w=1366, h=173
      fill=surface/secondary
      paddingLeft=16, paddingRight=16, paddingTop=12, paddingBottom=12
      layoutMode=VERTICAL, itemSpacing=8

      Frame: LaTeXRow  layoutMode=HORIZONTAL, itemSpacing=8, h=40
        Icon: "ƒ" (20pt, text/secondary)
        Text: display-mono/32 "sin(π) + 2^8", fill=text/primary, truncate

      Frame: UnicodeRow  layoutMode=HORIZONTAL, itemSpacing=8, h=40
        TextInput: w=fill, h=40, cornerRadius=8, stroke=surface/tertiary
          fill=white, paddingH=12
          Placeholder: body/15 "sin(π) + 256", fill=text/secondary
          Editable — user can correct transcription

      Frame: ErrorRow (hidden by default, visible on recognition failure)
        layoutMode=HORIZONTAL, itemSpacing=8, h=24
        Icon: "⚠" (14pt, destructive)
        Text: caption/11 "Recognition failed — retry or edit manually", fill=destructive

      Btn: "Send"  x=16, y=bottom-12, w=1334, h=44, cornerRadius=10
        fill=accent
        label: button-label/15 "Send to Desktop", fill=white

    ── Loading Overlay (during recognition) ──
    Frame: RecognizingOverlay  x=0, y=0, w=1366, h=720
      fill=canvas/background (60% opacity)

      Frame: SpinnerCard  center of overlay, w=160, h=80, cornerRadius=16
        fill=white
        shadow: 0 4px 24px rgba(0,0,0,0.12)
        layoutMode=VERTICAL, center-center, itemSpacing=8

        Spinner: 32×32 circle (animate: stroke dash, accent color)
        Text: caption/11 "Recognising…", fill=text/secondary

  TabBar [Tab 4 "Write" active]
```

---

## Screen 5 — Settings (Tab 5 active)

```
Frame: Screen/Settings  1366×1024

  StatusBanner [Connected state or disconnected variant]

  Frame: Content  x=0, y=44, w=1366, h=897
    fill=surface/primary

    ScrollView content (total height ~1400pt, scrollable):
      paddingLeft=20, paddingRight=20, paddingTop=16, paddingBottom=32
      layoutMode=VERTICAL, itemSpacing=24

      ── Section: Connection ──
      SectionHeader: "Connection" (heading/20, text/primary)

      FormCard  w=fill, cornerRadius=12, fill=surface/secondary, paddingAll=16
        layoutMode=VERTICAL, itemSpacing=0

        Row: "Server Host"
          h=56, layoutMode=HORIZONTAL, counterAxisAlignItems=CENTER
          Label: setting-label/17, fill=text/primary, w=200
          Input: w=fill, h=36, cornerRadius=8
            stroke=surface/tertiary, fill=white, paddingH=12
            value: body/15 "192.168.1.100", fill=text/primary

        Divider: h=1, fill=surface/tertiary

        Row: "Port"
          h=56, same layout
          Input: value "8765"

        Divider

        Row: "Reconnect"  h=56
          Label: setting-label/17 "Reconnect"
          [spacer]
          Btn: "Reconnect" w=120, h=36, cornerRadius=8, fill=accent
            label: button-label/15 "Reconnect", fill=white

      ── Section: Tilt Navigation ──
      SectionHeader: "Tilt Navigation"

      FormCard:
        Row: Toggle "Enable Tilt"  h=56
          Label: setting-label/17
          Toggle: iOS-style, accent tint

        Divider

        SliderRow: "Sensitivity"  h=72
          Label: setting-label/17
          Slider: min=0.1, max=5.0, value=1.0, accent tint, w=fill
          ValueLabel: caption/11 "1.0", fill=text/secondary, w=40, trailing

        Divider

        SliderRow: "Dead Zone"  h=72
          Slider: min=0.005, max=0.1, value=0.02
          ValueLabel: "0.02"

      ── Section: Gaze & Dwell ──
      SectionHeader: "Gaze & Dwell"

      FormCard:
        Toggle "Enable Gaze"
        Divider
        SliderRow "Dwell Timeout"  min=0.3s, max=3.0s, value=1.0s, ValueLabel="1.0s"

      ── Section: Head Tracking ──
      SectionHeader: "Head Tracking"

      FormCard:
        Toggle "Enable Head Tracking"  (off by default)
        Divider
        SliderRow "Smoothing"  min=0.05, max=1.0, value=0.3, ValueLabel="0.3"

      ── Section: Trackpad ──
      SectionHeader: "Trackpad"

      FormCard:
        SliderRow "Speed"  min=0.5x, max=5.0x, value=2.0x, ValueLabel="2.0×"
        Divider
        SliderRow "Palm Reject Radius"  min=10pt, max=60pt, value=25pt, ValueLabel="25pt"

      ── Section: Voice Keywords ──
      SectionHeader: "Voice Keywords"

      FormCard:
        3 existing keywords (list rows, h=56 each):
          Row: "click"     [delete ✕ icon, destructive, trailing]
          Row: "scroll"    [delete ✕]
          Row: "open"      [delete ✕]
        Divider
        Row: "+ Add keyword…"  h=56
          label: setting-label/17 "+ Add keyword…", fill=accent

      ── Section: Sound Mappings ──
      SectionHeader: "Sound Mappings"

      FormCard (read-only in current implementation):
        Row: "cluck" → "CLICK"    h=56
          Label: setting-label/17 "cluck", fill=text/primary
          [spacer]
          Label: body/15 "CLICK", fill=text/secondary
        Row: "pop"   → "SCROLL down"
        Row: "hiss"  → "SCROLL up"

  TabBar [Tab 5 "Settings" active]
```

### SliderRow Sub-Component
```
Frame: SliderRow  w=fill, h=72
  layoutMode=VERTICAL, itemSpacing=4, paddingTop=12, paddingBottom=12

  Frame: LabelRow  layoutMode=HORIZONTAL, counterAxisAlignItems=CENTER
    Label: setting-label/17, fill=text/primary, w=fill
    ValueBadge: caption/11, fill=text/secondary, trailing

  Slider: w=fill, h=4, accent tint thumb, surface/tertiary track
```

---

## Component Summary

| Component | Size | Variants | Notes |
|-----------|------|----------|-------|
| StatusBanner | 1366×44 | Connected / Connecting / Disconnected | Swap fill only |
| CommandButton | 80×80 | Default / Pressed | 12pt radius, VStack icon+label |
| CalcKey | 333×134 (basic) / 265×80 (sci) | Number / Operator / Function / Send / Destructive | 10pt radius |
| TrackpadKey | fill×56 | Default / Pressed | 10pt radius, HStack |
| SliderRow | fill×72 | Default | VStack label+slider |
| FloatingCard | hug | — | regularMaterial, 12pt radius, shadow |
| TabItem | 273×83 | Active / Inactive | VStack icon+label+indicator dot |
| FormCard | fill×hug | — | 12pt radius, surface/secondary |

---

## Prototyping Connections

| Trigger | From | To |
|---------|------|----|
| Tap Tab 1 | Any screen | CommandPad |
| Tap Tab 2 | Any screen | Trackpad |
| Tap Tab 3 | Any screen | Keypad |
| Tap Tab 4 | Any screen | Handwriting |
| Tap Tab 5 | Any screen | Settings |
| Swipe left | CommandPad/Trackpad/Keypad | Next tab (page-style) |
| Swipe right | Trackpad/Keypad/Handwriting | Previous tab (page-style) |
| Drag left on tab bar | Any screen | Next tab |
| Drag right on tab bar | Any screen | Previous tab |
| Tap "Fullscreen" | Trackpad | Trackpad-Fullscreen |
| Tap "↙" | Trackpad-Fullscreen | Trackpad |
| Tap "Recognise" | Handwriting | Handwriting (recognizing overlay variant) |
| Receive result | Recognizing | Handwriting (result bar visible) |
| Tap "Edit" | CommandPad | CommandPad-EditMode |
| Tap "Done" | EditMode | CommandPad |

---

## Notes for Implementation

- All corner radii are consistent across matched component types: buttons=10pt, cards=12pt, banners=0pt (full-width)
- The trackpad surface has a subtle inner shadow: `inset 0 1px 3px rgba(0,0,0,0.06)` to communicate it's a touch zone
- The tab bar uses a `0.5pt top border` (surface/tertiary) as the only separator — no shadow
- The tab bar supports a horizontal drag gesture (60pt threshold) to switch tabs in either direction
- The first 4 tabs (Commands, Trackpad, Keypad, Write) use `.tabViewStyle(.page)` for swipe-to-switch; Settings and Sensors are tap-only
- All form rows use `h=56pt` minimum for accessibility (matches iOS 44pt touch target minimum + padding)
- Status banner must sit above the NavigationStack (not inside it) — it's always visible
- Keypad `display-mono/32` text right-aligns and truncates from the left when the expression is long (SwiftUI `ScrollView(.horizontal)`)
- Handwriting canvas background is pure white (#FFFFFF) even in dark environments — it represents paper
- Settings sliders show their current value inline as a `ValueLabel` (trailing position, caption/11)
