# ConsentVid Python API

`pip install consentvid` (or `pip install -e .` in a checkout) gives you the
`consentvid` CLI and this API. They are one implementation: every CLI
contributor command is a thin layer over `consentvid.api`, so the same rules
apply whichever you use:

- records are validated locally before anything is uploaded or written;
- an account with the `bot` self-label can't consent, and neither can one
  whose `community.lexicon.preference.ai` denies training (for a training
  consent);
- identifying metadata (GPS, device, serials) is stripped from files before
  they are hashed and uploaded (needs ffmpeg);
- a video and its consent are written in one atomic `applyWrites` commit,
  the consent under the video's own record key (its sidecar).

The API never prompts. Progress messages and warnings go to the
`consentvid` logger, or to `Session(..., on_message=callback)`.

## Sign in

```python
from consentvid import Session

s = Session.login()                              # cached session, else settings
s = Session.login("you.bsky.social", app_password)  # new session (cached, like `consentvid login`)
print(s.handle, s.did)
```

Without arguments, `login()` uses the session cached by `consentvid login`
(`~/.consentvid/session.txt`), else `CONSENTVID_HANDLE` and
`CONSENTVID_APP_PASSWORD` from the environment or
`~/.consentvid/config.env`. Use an **app password**
(bsky.app/settings/app-passwords), never your main password. Your PDS is found
from your handle, so the password only goes to the server that hosts your
account.

## Terms

```python
from consentvid import Terms

terms = Terms(
    purposes=["training", "evaluation"],        # what it may be used for
    modalities=["video", "audio"],              # what may be used
    model_classes=["academic", "open-weights"],  # who may use it (either spelling)
    people="onlyMe",          # onlyMe | allConsented | noIdentifiablePeople
    adult=True,               # you affirm you are an adult able to consent
    authorship=True,          # you affirm you recorded it / hold the rights
    expires="2y",             # optional: datetime, date, timedelta, "2027-01-01", "90d", "12w", "2y"
    compensation="none",      # declaration only; nothing pays or enforces it
    rights_basis="ownRecording",
)
```

All arguments are keyword-only, and every term without a default must be
given: nothing is granted by default. `adult` and `authorship` must be the
literal `True`, since they are affirmations signed by your key. Invalid
values raise `consentvid.UsageError` when the `Terms` object is created,
before anything touches the network. A relative expiry ("90d") is fixed at
that moment.

## One video

```python
clip = s.contribute("clip.mp4", terms, title="Making pasta", tags=["cooking"],
                    language="en", storage="r2")
clip.video_uri, clip.consent_uri, clip.content_hash, clip.storage
```

`storage`: `"r2"`, `"ipfs"` or `"peertube"` upload the stripped file;
`"url"` with `storage_value="https://..."` points at a copy you host (it must
already be stripped); `"local"` keeps the bytes on your disk (not usable in
shared datasets). The default is `$CONSENTVID_STORAGE`, else `r2` if
configured, else `local`. Other options: `description`, `self_labels`
(`porn`, `sexual`, `nudity`, `graphic-media`: datasets leave such clips out
unless asked), `keep_metadata=True` (not recommended), `allow_duplicate=True`.

Videos you already posted on Bluesky:

```python
s.contribute_from_post("https://bsky.app/profile/you.bsky.social/post/3abc...", terms)
```

The bytes stay in your post: deleting the post removes the clip from every
dataset.

Two steps, if you want them:

```python
video = s.register_video("clip.mp4", title="Making pasta", storage="r2")
consent = s.attach_consent(video.uri, terms)
s.attach_consent(video.uri, new_terms, replace=True)  # change the terms
```

## Withdraw

```python
s.revoke(clip.consent_uri)                      # withdraw the consent, keep the video record
s.revoke(clip.video_uri, delete_bytes=True)     # video + its consent in one commit, and the bytes

def confirm(plan):                              # optional: called before anything is deleted
    print(plan.kind, plan.consents, plan.video and plan.video.get("title"))
    return input("go? ") == "y"                 # False raises consentvid.Cancelled

s.revoke(clip.video_uri, confirm=confirm)
```

## Look

```python
for v in s.list():
    print(v.status, v.title, v.uri)   # consented | expired | no-consent | ignored-consent

r = s.check(clip.video_uri)           # or consentvid.check(uri): no login needed
r.status, r.reason, r.ok              # the verdict `consentvid resync` gives this clip
```

## Many videos

`contribute_many` contributes a list of videos one at a time (never in
parallel: one repo, one writer) and returns one `BulkResult` per item.

```python
import csv
import logging
from pathlib import Path

from consentvid import BulkItem, Session, Terms

logging.basicConfig(level=logging.INFO)   # see progress

s = Session.login()
terms = Terms(purposes=["training"], modalities=["video", "audio"], model_classes=["academic"],
              people="onlyMe", adult=True, authorship=True)

# One row per clip: path, title, tags, people ("onlyMe" or "allConsented").
items = []
with open("clips.csv") as f:
    for row in csv.DictReader(f):
        clip_terms = terms if row["people"] == "onlyMe" else Terms(
            purposes=["training"], modalities=["video"], model_classes=["academic"],
            people="allConsented", adult=True, authorship=True)
        items.append(BulkItem(path=Path(row["path"]), title=row["title"],
                              tags=row["tags"].split(";") if row["tags"] else (), terms=clip_terms))

results = s.contribute_many(items, storage="r2", resume="clips.run.jsonl", on_error="continue",
                            progress=lambda r: print(r.status, r.key, r.error or ""))

failed = [r for r in results if not r.ok]
print(f"{sum(r.status == 'contributed' for r in results)} contributed, "
      f"{sum(r.status == 'skipped' for r in results)} skipped, {len(failed)} failed")
```

Items can be paths, `BulkItem`s, or dicts with `BulkItem`'s fields (`path` or
`post`, `title`, `description`, `language`, `tags`, `self_labels`, `terms`,
`storage_value`). Each item's terms come from `item.terms`, else
`terms_for(item)`, else `terms=`; its title from `item.title`, else
`title_for(item)`. File names are never used as titles: camera file names
(`PXL_20261106_101530123.mp4`) give away when and with what a clip was shot.

- **Check every clip yourself.** A bulk run makes it easy to consent for
  videos nobody looked at. Only include videos you recorded (or hold the
  rights to), and set `people` per video: `onlyMe` only if nobody else is
  identifiable, `allConsented` only if everyone shown agreed to this use.
- **Resume.** With `resume="run.jsonl"`, every outcome is appended to that
  file as it happens. Re-running the same call skips the items it records as
  done, as long as the file at that path still has the same contents (an
  edited file is contributed again). If the run died between a write and
  its log line, the re-run finds the bytes already registered and reports
  them as `skipped`. Nothing is written twice.
- **Write budget.** A contribution is two record creates: 6 points of the
  PDS's write budget (bsky.social: 5000 points per hour, 35000 per day, per
  account). The run paces itself under `points_per_hour=5000` and
  `points_per_day=35000`, which is about 830 videos an hour. Set them to your
  PDS's limits, or `None` to turn pacing off. If the PDS still answers 429,
  the run waits until its reset when that is at most `max_rate_limit_wait`
  seconds (default one hour) away, and otherwise stops: re-run later with the
  same `resume` file.
- **Errors.** `on_error="continue"` (the default) records a failed item and
  goes on; `"stop"` stops at the first failure. Network errors are retried
  (`retries=3`, with backoff). A write whose answer was lost is looked up
  under its record key before anything is retried.
- **Uploads.** R2 objects are content-addressed, so an upload repeated after
  a crash reuses the same object.

## Errors

All expected failures are subclasses of `consentvid.ConsentVidError`, with a
message meant for people:

| Exception | When |
| --- | --- |
| `UsageError` | a bad argument: unknown term value, missing title, bad URI |
| `ConfigError` | not logged in, missing storage settings, no ffmpeg |
| `AlreadyRegistered` | these bytes are already one of your videos (`.uri`) |
| `RateLimited` | the PDS refused a write for now (`.reset`: epoch seconds) |
| `Cancelled` | a `confirm` callback said no; nothing was changed |
| `RepoUnavailable` | the account is deactivated, taken down or deleted |

Some messages name the equivalent CLI flag (`--keep-metadata`,
`--allow-duplicate`); in the API that is the keyword argument of the same
name.
