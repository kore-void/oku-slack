# OKÚ voice recording guide (XTTS-v2 fine-tune)

Custom voices for the six OKÚ Slack personas are trained **only on recordings made by Kore and consenting friends performing the characters**. Never add source audio of real politicians or any third party, not even "just as a reference". Recordings stay local (`voice/raw/` is git-ignored).

## Personas & scripts

| key | persona | script |
|---|---|---|
| `babis` | Babiš | `voice/scripts/babis.md` |
| `alenka` | Alenka Hranolka | `voice/scripts/alenka.md` |
| `bourak` | Filip „Bourák“ Turek | `voice/scripts/bourak.md` |
| `marty` | Marty Prchal | `voice/scripts/marty.md` |
| `peta` | Peťa Maci | `voice/scripts/peta.md` |
| `kalousek` | Kalousek | `voice/scripts/kalousek.md` |

Each script runs to roughly 150–190 numbered Czech lines, ~1300 words, ~10 minutes of reading. A single read gives about 8–10 min of clean audio. Aim for **10–30 min per voice**, so record the script 1–3 times (different sessions or moods are fine) or improvise extra in-character lines.

## Consent (mandatory)

Before anything else, every performer records this line as `<key>_000.wav`, in their own name:

> „Já, [jméno], souhlasím s použitím svého hlasu pro OKÚ boty.“

If one person performs several personas, they record it once per persona folder. If there is no consent file, that voice does not get trained.

## Equipment & room

- Use a USB/XLR condenser or dynamic mic. A headset is a last resort. Sit 15–25 cm away, slightly off-axis, with a pop filter.
- Pick a small, soft room (sofa, curtains, closet with clothes). Turn off fans, AC, fridge hum and notifications.
- Keep the same mic, position and room for the whole persona. Consistency matters more than gear.
- Drink water and avoid dairy right before. Take breaks every ~10 min.
- Act the character with your voice (energy, tempo, attitude), but don't shout into the mic or whisper inaudibly. For "whisper-ish" lines, use a calm, low, still-voiced delivery.

## Format

- **WAV, mono, 44.1 kHz or 48 kHz, 24-bit (16-bit OK)**. Don't use MP3 or any lossy format.
- Set input gain so **peaks land at -12 to -6 dBFS**. Never clip. Leave ~0.5 s of silence before and after each line.
- Don't apply noise reduction, compression or EQ at record time. Light cleanup happens in preprocessing.

## Takes & naming

Choose either option (A is preferred):

- **A. One file per sentence:** `<key>_<nnn>.wav`, where `nnn` is the 3-digit line number from the script (e.g. `babis_042.wav`). If you redo a line, overwrite the file or add a suffix (`babis_042b.wav`); the last take wins.
- **B. Long takes:** `<key>_take<nn>.wav` (e.g. `peta_take01.wav`). Read the lines in order with **~1 s pause** between them. Say the line number out loud only if you lose your place (it gets cut). The pipeline segments on silence and aligns to the script with Whisper.

Extra improvised lines: `<key>_x<nnn>.wav`, plus a matching `<key>_x<nnn>.txt` containing the exact transcript.

## Folder layout

```
voice/
  RECORDING.md
  scripts/<key>.md          # committed
  raw/<key>/                # NOT committed (.gitignore)
    <key>_000.wav           # consent line
    <key>_001.wav ... <key>_nnn.wav
    notes.txt               # performer name, date, mic, anything odd
```

## Fine-tune plan (umbra)

- Machine: **umbra**, NVIDIA **RTX 3060 12 GB**, dedicated *voice* venv with **coqui-tts 0.27.5** (XTTS-v2 base model, language `cs`).
- Pipeline per voice:
  1. Check the consent file exists. Convert to 22.05 kHz mono for XTTS training (keep the originals).
  2. Segment and trim silence, normalise loudness, and drop clipped or noisy clips. Target clip length is 2–12 s.
  3. Transcribe and verify against the script (Whisper). Build `metadata.csv` (LJSpeech-style: `file|text|speaker`).
  4. Fine-tune XTTS-v2 GPT decoder (Coqui `recipes/ljspeech/xtts_v2` style). Small batch size plus gradient accumulation to fit 12 GB, ~6–15 epochs, with a held-out eval of ~5 % of the clips.
  5. Listen to test sentences, pick the best checkpoint, and export to `voice/models/<key>/` (git-ignored, local only).
- **Data:** 10–30 min of clean audio per voice.
- **Time estimate:** roughly **1–3 hours of training per voice** on the 3060, plus ~15–30 min of preprocessing. That works out to about one evening for all six if run back-to-back.
- Nothing is deployed to the bots automatically. Wiring the voices into OKÚ is a separate, explicit step.

---

## Rychlé shrnutí (česky)

- Trénujeme **jen na vašich vlastních nahrávkách** (Kore + kamarádi se souhlasem). Žádné nahrávky skutečných politiků.
- Na začátku vždy nahrajte souhlas: **„Já, [jméno], souhlasím s použitím svého hlasu pro OKÚ boty.“** → `<key>_000.wav`.
- Potřebujete tichou a měkkou místnost, mikrofon 15–25 cm od úst a pop filtr. Vypněte větrák a notifikace.
- Formát je **WAV mono, 44,1 nebo 48 kHz**, špičky **-12 až -6 dB** a nesmí to clipovat. Žádné efekty.
- Buď **jeden soubor na větu** `<key>_<nnn>.wav` (číslo věty ze skriptu), nebo dlouhý take s pauzou ~1 s mezi větami.
- Ukládejte do `voice/raw/<key>/` (není v gitu).
- Jeden skript je ~10 min čtení. Cíl je 10–30 min audia na hlas, takže skript přečtěte 1–3×.
- Trénink poběží na umbře (RTX 3060 12 GB, coqui-tts 0.27.5, XTTS-v2) a zabere asi 1–3 h na hlas. Nic se automaticky nenasazuje.
