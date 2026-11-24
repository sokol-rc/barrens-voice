"""The voice bank for F5-TTS: reference clips made from the recordings in the game client itself.

The client's Russian voice-over has, per race and sex, the player character's voice (error
messages such as "Недостаточно маны", and calm emotes) and several NPC voice types (greetings,
farewells). `game_voices.json` (made by tools/game_voices.py) lists which files those are; here
they are read out of the local CASC storage (read only: `casc.py`), heard by Whisper for their
text (the client has no subtitles for them), and a few calm lines of each voice are put together
into one reference clip of 5..10 s. Each voice also gets copies shifted in pitch, so characters of
one race and sex can sound apart. Nothing of the game is shipped: the bank is made on the player's
machine, once per game build (kept in voices/ with the build key it came from).
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from .dictionary import wow_roots

BANK_VERSION = 6
SAMPLE_RATE = 24000
TABLE = Path(__file__).with_name("game_voices.json")
GAP = 0.15            # seconds of silence between the lines of a reference clip
TARGET = (5.5, 9.5)   # seconds of speech wanted in a reference clip
MAX_LINES = 4
MIN_SPEECH = 3.0      # seconds: a voice with less usable speech than this gets no reference clip
PROGRESS_EVERY = 10.0  # seconds between the bank's progress lines (the launcher shows the last one only)
GOOD = 0.8            # the joined clip heard back by Whisper must match its text at least this well
# Whisper's hallucinations on short or wordless clips
JUNK = re.compile(r"продолжение следует|субтитр|спасибо за просмотр|редактор|корректор|подпис", re.I)


# The races of WoW Forever (the table also has the ones retail added: Forever's client holds their
# files too, but no player there speaks with them).
FOREVER_RACES = {"human", "orc", "dwarf", "nightelf", "scourge", "tauren", "gnome", "troll", "skyborne"}
FOREVER_ONLY = {"skyborne"}   # Forever's own race: retail has neither the race nor its files


@dataclass(frozen=True)
class Slot:
    """One voice a character can be given: a reference clip, its text and how long the speech in it
    is (without the pauses between its lines: the pace of the voice)."""
    id: str          # "orc-male", "orc-male/standard", "orc-male/standard^+1.5" for a pitch-shifted copy
    base: str        # the voice it is a copy of
    race: str        # "orc", "scourge", ...
    sex: str         # "male", "female"
    shift: float     # semitones
    path: str
    text: str
    speech: float = 0.0


def find_game(config: dict) -> tuple[Path, Path] | None:
    """(the WoW install with the storage, its game folder) - Forever first. The game folder must
    exist and its product be installed there (one install can hold several games, and several
    installs can sit on different drives)."""
    from .casc import build_info, product_of

    def usable(root: Path) -> bool:
        if not (root.is_dir() and (root.parent / ".build.info").exists() and (root.parent / "Data").is_dir()):
            return False
        try:
            build_info(root.parent, product_of(root))
            return True
        except (LookupError, OSError):
            return False

    roots = wow_roots(config)                          # only the chosen game's folders
    for root in roots:
        if usable(root):
            return root.parent, root
    return None


def other_storages(config: dict, log) -> list:
    """The storages of the other WoW games installed: retail keeps much of its data on Blizzard's
    servers until the game needs it, so the voices of the classic races may not be on its disk,
    while Forever has the very same files (the same content keys)."""
    from .casc import Storage
    from .dictionary import GAMES, game_of
    out = []
    for game in GAMES:
        if game == game_of(config):
            continue
        found = find_game({**config, "game": game, "wow_dir": ""})
        if found:
            try:
                from .casc import product_of
                out.append(Storage(found[0], product_of(found[1])))
            except (LookupError, OSError, ValueError) as e:
                log(f"хранилище {found[1]}: {e}")
    return out


def bank_dir(config: dict, home: Path) -> Path:
    """Each game its own bank: the clients' files and builds differ (voices, voices-retail)."""
    from .dictionary import game_of
    return home / ("voices" if game_of(config) == "forever" else "voices-" + game_of(config))


def voices_for(config: dict) -> dict:
    """The voice table's entries for the chosen game's races."""
    from .dictionary import game_of
    table = json.loads(TABLE.read_text(encoding="utf-8"))["voices"]
    if game_of(config) == "forever":
        table = {v: e for v, e in table.items() if e["race"] in FOREVER_RACES}
    else:
        table = {v: e for v, e in table.items() if e["race"] not in FOREVER_ONLY}
    return table


def _stamp(config: dict, shifts) -> dict | None:
    """What the bank depends on; None when no game is found."""
    from .casc import build_info, product_of
    found = find_game(config)
    if not found:
        return None
    install, flavor = found
    product = product_of(flavor)
    try:
        build = build_info(install, product)["Build Key"]
    except (LookupError, KeyError, OSError):
        return None
    return {"version": BANK_VERSION, "install": str(install), "product": product, "build": build,
            "shifts": list(shifts),
            "table": zlib.crc32(json.dumps(voices_for(config), sort_keys=True).encode("utf-8"))}


GROWN = 100 * 1024 * 1024   # bytes the game's storage must grow by before an incomplete bank is made again


def data_size(install: Path) -> int:
    """Bytes in the game's storage archives (Data/data/data.NNN): retail downloads the rest of the game
    in the background after it is playable, so this grows until it has everything."""
    try:
        return sum(p.stat().st_size for p in (Path(install) / "Data" / "data").glob("data.*"))
    except OSError:
        return 0


def load(out: Path, stamp: dict | None) -> list[Slot] | None:
    """The bank as made for `stamp`; None when it has to be made (again): none, another stamp, a
    damaged bank.json, a reference clip gone from the folder."""
    manifest = out / "bank.json"
    if stamp is None or not manifest.exists():
        return None
    try:
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        if saved.get("stamp") != stamp:
            return None
        # made while voice files were not on the disk yet (retail before its download finished): made
        # again once the storage has grown - a complete bank never is, whatever the storage does
        gap = saved.get("incomplete")
        if gap and data_size(Path(stamp["install"])) > gap.get("data_size", 0) + GROWN:
            return None
        slots = [Slot(**s) for s in saved["slots"]]
        whole = all(Path(s.path).is_file() for s in slots)     # a path of another type (5, null) raises here
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return slots if whole else None


def stale(config: dict, out: Path) -> bool:
    """Whether the bank has to be (re)made: no bank, another game build, other settings."""
    stamp = _stamp(config, config.get("voice_shifts", []))
    return stamp is not None and load(out, stamp) is None


# --- choosing the lines -------------------------------------------------------------------------

def plausible(text: str, seconds: float) -> bool:
    """A transcript worth trusting: Russian words at a speaking pace. Short shouts and wordless
    sounds come back from Whisper as made-up sentences ("Заг-заг" as a whole phrase)."""
    text = text.strip()
    letters = len(re.findall(r"[а-яё]", text, re.I))
    if not letters or JUNK.search(text) or re.search(r"[a-z]{3}", text, re.I):
        return False
    if re.search(r"(ха-?){2}|ля-ля|хей", text, re.I):
        return False
    return 0.9 <= seconds <= 7.5 and 5 <= letters / seconds <= 22


def choose(lines: list[dict]) -> list[dict]:
    """The lines for a reference clip from heard ones ({"text", "seconds", "priority"}), in order:
    the calmest first, up to MAX_LINES and TARGET seconds of speech; at most one question, and the
    clip ends with a statement (F5 continues the reference: its last line sets the tone)."""
    picked: list[dict] = []
    total = 0.0
    question = False
    for line in sorted(lines, key=lambda x: (x["priority"], abs(x["seconds"] - 3.0))):
        if len(picked) >= MAX_LINES or total >= TARGET[0]:
            break
        is_q = line["text"].rstrip().endswith("?")
        if (is_q and question) or total + line["seconds"] > TARGET[1]:
            continue
        picked.append(line)
        total += line["seconds"]
        question = question or is_q
    statements = [x for x in picked if not x["text"].rstrip().endswith("?")]
    if statements and picked[-1] not in statements:
        picked.remove(statements[-1])
        picked.append(statements[-1])
    return picked


def joined_text(lines: list[dict]) -> str:
    texts = []
    for line in lines:
        t = re.sub(r"[«»\"„“”]", "", line["text"]).strip()
        texts.append(t if t[-1:] in ".!?…" else t + ".")
    return " ".join(texts)


# --- audio --------------------------------------------------------------------------------------

def _decode(data: bytes):
    import numpy as np
    import soundfile as sf

    samples, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    mono = samples.mean(axis=1)
    if rate != SAMPLE_RATE:
        mono = np.interp(np.linspace(0, len(mono) - 1, int(len(mono) * SAMPLE_RATE / rate)),
                         np.arange(len(mono)), mono).astype("float32")
    return _trim(mono)


def _trim(x):
    import numpy as np

    a = np.abs(x)
    if not len(a) or a.max() == 0:
        return x
    loud = np.flatnonzero(a > a.max() * 0.03)
    pad = int(0.03 * SAMPLE_RATE)
    return x[max(0, loud[0] - pad):loud[-1] + pad]


def _join(clips):
    """The lines one after another, each at the same loudness, with short pauses."""
    import numpy as np

    gap = np.zeros(int(GAP * SAMPLE_RATE), dtype="float32")
    parts = []
    for x in clips:
        parts += [x / (np.sqrt(np.mean(x ** 2)) + 1e-9) * 0.08, gap]
    wav = np.concatenate(parts[:-1])
    peak = float(np.abs(wav).max())
    return wav / peak * 0.95 if peak > 0.95 else wav


def _shift(samples, semitones: float):
    """Pitch shift by resampling: a little higher (or lower) and a little faster (or slower) -
    for a reference clip that is only a different, still natural voice."""
    import numpy as np

    factor = 2 ** (semitones / 12)
    n = int(len(samples) / factor)
    return np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples).astype("float32")


# --- the bank -----------------------------------------------------------------------------------

def took(seconds: float) -> str:
    return f"{round(seconds)} с" if seconds < 90 else f"{round(seconds / 60)} мин"


def build(config: dict, out: Path, log, checker, clock=time.monotonic) -> list[Slot]:
    """The bank: kept as it is while the game build and settings are the same, else made again
    from the client with `checker` (a loaded whisper_onnx.OnnxChecker: Whisper). Made: once, and after
    each game update or a change of `voice_shifts`; its progress goes to the log every PROGRESS_EVERY s
    ("собираю голоса рас: N из M"), then "голоса рас собраны" (the launcher keeps one line of them)."""
    shifts = config.get("voice_shifts", [])
    stamp = _stamp(config, shifts)
    if stamp is None:
        raise RuntimeError("не найдена папка игры (укажи wow_dir в config.json)")
    slots = load(out, stamp)
    if slots is not None:
        return slots
    if checker is None:
        raise RuntimeError("для голосов из клиента нужна модель Whisper (распознать текст реплик)")
    import soundfile as sf

    from .casc import Storage

    started = told = clock()
    log("собираю голоса рас из игры — несколько минут ...")
    log(f"    {stamp['install']}, {stamp['product']}: один раз после каждого обновления игры")
    table = voices_for(config)
    storage = Storage(Path(stamp["install"]), stamp["product"])
    others = other_storages(config, log)
    files = storage.read_files({f[0] for v in table.values() for f in v["files"]}, "ruRU", others)
    borrowed = getattr(storage, "borrowed", 0)
    log(f"русских реплик в клиенте: {len(files)}" + (f", из них {borrowed} — из другой установленной игры WoW (те же "
                                                      "файлы: этот клиент их ещё не скачал)" if borrowed else ""))
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.wav"):
        old.unlink()
    slots: list[Slot] = []
    skipped = []
    for done, (voice, entry) in enumerate(sorted(table.items())):
        if clock() - told >= PROGRESS_EVERY:
            told = clock()
            from . import state

            state.report(bank_progress=f" ({done}/{len(table)})")
            log(f"собираю голоса рас: {done} из {len(table)}")
        heard: list[dict] = []
        audio: dict[int, object] = {}
        candidates = [f for f in entry["files"] if f[0] in files]
        # the calmest first, then lines of about 3 s; heard one by one only until there are enough
        candidates.sort(key=lambda f: f[2])
        for fdid, _tag, priority in candidates:
            try:
                x = _decode(files[fdid])
            except Exception:  # noqa: BLE001 - one unreadable file does not matter
                continue
            seconds = len(x) / SAMPLE_RATE
            if not 0.9 <= seconds <= 7.5:
                continue
            text = checker.transcribe(x, SAMPLE_RATE).strip()
            if _tag == "line" and not text.endswith("."):
                # a numbered line (retail's allied races: what it is, is not known): a battle cry ("!")
                # or a joke ("?") makes the whole voice shout or flirt - the calm ones first
                priority = 3
            if plausible(text, seconds):
                heard.append({"fdid": fdid, "text": text, "seconds": seconds, "priority": priority})
                audio[fdid] = x
            # enough of the calmest kind the voice has: stop listening (else on to the end)
            if sum(h["seconds"] for h in heard if h["priority"] == candidates[0][2]) >= TARGET[1] * 1.5:
                break
        made = None
        pool = list(heard)
        for _ in range(3):
            lines = choose(pool)
            if sum(x["seconds"] for x in lines) < MIN_SPEECH:
                break
            wav, text = _join([audio[x["fdid"]] for x in lines]), joined_text(lines)
            score, _ = checker.score(wav, SAMPLE_RATE, text)
            if score >= GOOD:
                made = (wav, text, sum(x["seconds"] for x in lines))
                break
            pool.remove(min(lines, key=lambda x: x["seconds"]))   # the shortest line is the likeliest wrong
        if made is None:
            skipped.append(voice)
            continue
        wav, text, speech = made
        safe = voice.replace("/", "_")
        for shift in [0.0] + list(shifts):
            slot_id = voice if shift == 0 else f"{voice}^{shift:+g}"
            path = out / (f"{safe}.wav" if shift == 0 else f"{safe}_{shift:+g}.wav")
            sf.write(str(path), wav if shift == 0 else _shift(wav, shift), SAMPLE_RATE)
            # a shifted copy is played faster or slower by the same factor
            slots.append(Slot(slot_id, voice, entry["race"], entry["sex"], shift, str(path), text,
                              round(speech / 2 ** (shift / 12), 3)))
    missing = len(getattr(storage, "missing", []))
    manifest = {"stamp": stamp, "slots": [s.__dict__ for s in slots]}
    if missing:
        manifest["incomplete"] = {"missing_files": missing, "data_size": data_size(Path(stamp["install"]))}
        log("игра скачала не все голоса — банк доберётся позже")
        log(f"    ещё нет: {missing}")
    # through a temporary file: a companion stopped while writing leaves no half a bank.json
    tmp = out / "bank.json.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out / "bank.json")
    log(f"голоса рас собраны: {len({s.base for s in slots})} из {len(table)} за {took(clock() - started)}")
    log(f"голосов рас: {len({s.base for s in slots})}, вариантов с тембром: {len(slots)}"
        + (f". Без голоса (мало реплик): {', '.join(skipped)}" if skipped else ""))
    return slots
