"""ConsentVid command-line interface.

Contributor commands write to your own ATproto repo and need `consentvid
login`. Everything that only reads (verify, resync, export --repo, embed/read
C2PA, appview) talks directly to the relevant PDS and needs no account.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import click
from atproto_client.exceptions import AtProtocolError

from consentvid import __version__, _core, versions
from consentvid._core import (
    _check_session_scope,
    _default_storage,
    _env_file_too_open,
    _env_files,
    _get_record_or_none,
    _list_records,
    _require_attestation,
    _safe,
    _safe_lines,
    _validate_flag,
    _warn,
    _xrpc_error_name,
    _xrpc_message,
)
from consentvid.errors import ConfigError, ConsentVidError
from consentvid.errors import UsageError as _UsageError
from consentvid.records import (
    CONSENT_NSIDS,
    RECORD_NSIDS,
    VIDEO_FOR_CONSENT,
    VIDEO_NSID,
    VIDEO_NSIDS,
    VIDEO_V2_NSID,
    format_dt,
    hash_file,
    is_expired,
    now_iso,
    parse_at_uri,
    to_dict,
)
from consentvid.validate import validate_in_repo
from consentvid.vocab import (
    CAPTURE_PROVENANCE,
    COMPENSATION,
    MODALITIES,
    MODEL_CLASSES,
    OFFERED_MODEL_CLASSES,
    OFFERED_PURPOSES,
    PEOPLE_PRESENT,
    PURPOSES,
    RIGHTS_BASIS,
    SENSITIVE_SELF_LABELS,
    STORAGE_KINDS,
)

# ------------------------------------------------------------ plumbing -------
# The contributor core (sessions, settings, every write) lives in
# consentvid._core and is shared with the Python API (consentvid.api): the
# contributor commands below parse options, prompt, and print; the API does
# the work.


def _click_reporter(level: str, message: str) -> None:
    """Print the core's progress messages the way the CLI always has."""
    if level == "warning":
        click.secho(f"warning: {message}", fg="yellow", err=True)
    elif level == "success":
        click.secho(message, fg="green")
    else:
        click.echo(message)


class _CLI(click.Group):
    """Turn expected failures into one-line errors instead of tracebacks."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except _UsageError as e:
            if e.param_hint:
                raise click.BadParameter(_safe_lines(str(e)), param_hint=e.param_hint) from e
            raise click.UsageError(_safe_lines(str(e))) from e
        except ConsentVidError as e:
            raise click.ClickException(_safe_lines(str(e))) from e
        except AtProtocolError as e:
            raise click.ClickException(_xrpc_message(e)) from e


def _confirm_strict_validation(validate) -> None:
    """--validate would be refused by the PDS only after the bytes are
    uploaded: say so first, and go on only if the user insists."""
    if _validate_flag(validate) is not True:
        return
    _warn(
        "--validate (or CONSENTVID_VALIDATE) will likely be rejected by your PDS after the upload "
        "('Unknown lexicon type': no PDS resolves third-party lexicons yet); the bytes would then be cleaned up"
    )
    if not _interactive() or not click.confirm("Continue with strict PDS validation?", default=False):
        raise ConsentVidError("not uploading: drop --validate / unset CONSENTVID_VALIDATE")


_TERM_PROMPTS = (
    ("modalities", "--modality", MODALITIES, "What may be used"),
    ("purposes", "--purpose", OFFERED_PURPOSES, "What it may be used for"),
    ("model_classes", "--model-class", OFFERED_MODEL_CLASSES, "Who may use it"),
)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt_multi(label: str, choices) -> tuple:
    choice_str = ", ".join(choices)
    while True:
        raw = click.prompt(f"{label} (comma-separated; options: {choice_str})", default="", show_default=False)
        vals = tuple(dict.fromkeys(v.strip() for v in raw.split(",") if v.strip()))
        bad = [v for v in vals if v not in choices]
        if vals and not bad:
            return vals
        click.echo(f"  pick at least one of: {choice_str}" + (f" (unknown: {', '.join(bad)})" if bad else ""))


def _require_terms(terms: dict) -> None:
    """Consent must be specific (GDPR Art. 4(11)): nothing is granted by
    default. Missing terms are asked for interactively, else refused. Runs
    before any login or upload."""
    _require_attestation(terms["attest"])
    missing = [t for t in _TERM_PROMPTS if not terms.get(t[0])]
    if not terms.get("people"):
        missing.append(("people", "--people", PEOPLE_PRESENT, None))
    if missing and not _interactive():
        raise click.UsageError(
            "consent terms must be stated explicitly; nothing is granted by default. Missing: "
            + ", ".join(flag for _, flag, _, _ in missing)
        )
    for key, _flag, choices, label in missing:
        if key == "people":
            click.echo(
                "Who identifiably appears or is heard in the video?\n"
                "  onlyMe               only you\n"
                "  allConsented         others too, and every one of them agreed to this use\n"
                "  noIdentifiablePeople no identifiable face or voice (e.g. hands-only footage)"
            )
            terms["people"] = click.prompt("peoplePresent", type=click.Choice(PEOPLE_PRESENT))
        else:
            terms[key] = _prompt_multi(label, choices)
    if not terms.get("adult"):
        if not _interactive():
            raise click.UsageError(
                "consent requires --attest-adult: you affirm (signed by your key) that you are "
                "18 or older (or the age of majority where you live) and able to consent."
            )
        if not click.confirm("I am 18 or older (or the age of majority where I live) and able to consent"):
            raise click.UsageError("only adults able to consent may contribute")
        terms["adult"] = True
    if terms["people"] == "noIdentifiablePeople" and {"face", "voice"} & set(terms["modalities"]):
        raise click.UsageError("--modality face/voice contradicts --people noIdentifiablePeople")


# ------------------------------------------------------------- options -------


def _normalise_language(ctx, param, value):
    """Lowercase the primary subtag ('EN-gb' -> 'en-gb'): atproto readers
    reject an uppercase one."""
    if not value:
        return value
    primary, sep, rest = value.partition("-")
    return primary.lower() + sep + rest


def _video_options(f):
    for opt in reversed(
        [
            click.option(
                "--title", default=None, help="Short title: max 300 characters. Required, except with "
                "--from-post (default: the post's first line)."
            ),
            click.option(
                "--description",
                default="",
                help="Optional longer description: max 3000 characters.",
            ),
            click.option(
                "--language",
                default="",
                callback=_normalise_language,
                help="BCP-47 tag of spoken language, e.g. en, cs, pt-BR.",
            ),
            click.option(
                "--tag",
                "tags",
                multiple=True,
                help="Topic tag, max 64 characters. Repeatable, at most 8.",
            ),
            click.option(
                "--self-label",
                "self_labels",
                multiple=True,
                type=click.Choice(SENSITIVE_SELF_LABELS),
                help="Label your own video (Bluesky's global self-labels). Dataset tools leave labeled "
                "clips out unless asked to include them. Repeatable.",
            ),
            click.option(
                "--storage-kind",
                type=click.Choice(STORAGE_KINDS),
                default=None,
                help="Where the bytes live. 'pds' uploads the video as a blob to your own "
                "Bluesky PDS (no post; up to 300 MB, less on some PDSes; withdrawing frees it). "
                "Default: $CONSENTVID_STORAGE, else r2 if configured, else local (not usable in "
                "shared datasets).",
            ),
            click.option(
                "--storage-value",
                default=None,
                help="Existing pointer (URL or CID). Omit for r2/ipfs/peertube/pds to upload. "
                "The file must already be stripped of metadata: if not, a clean copy is "
                "written next to it for you to host instead.",
            ),
            click.option(
                "--keep-metadata",
                is_flag=True,
                help="Do NOT strip location/device metadata from the file (not recommended). "
                "Without it every clip is stripped, whatever the storage kind (needs ffmpeg).",
            ),
            click.option(
                "--allow-duplicate",
                is_flag=True,
                help="Register even if you already registered identical bytes.",
            ),
        ]
    ):
        f = opt(f)
    return f


def _refuse_attested(ctx, param, value):
    """'attested' is documented as capture-time cryptographic provenance, but
    nothing verifies it yet: writing or filtering on it would pass a
    self-declared string off as proof (R3-05)."""
    values = value if isinstance(value, tuple) else (value,)
    if "attested" in values:
        raise click.BadParameter(
            "'attested' is reserved for capture-time cryptographic provenance (C2PA/CAWG bound to "
            "your DID), which ConsentVid cannot verify yet; use selfAttested.",
            ctx=ctx,
            param=param,
        )
    return value


def _consent_options(f):
    for opt in reversed(
        [
            click.option(
                "--attest-authorship",
                "attest",
                is_flag=True,
                help="REQUIRED. You affirm, signed by your key, that you recorded this "
                "video (or otherwise hold the rights) and may grant consent.",
            ),
            click.option(
                "--modality",
                "modalities",
                multiple=True,
                type=click.Choice(MODALITIES),
                help="What may be used. Repeatable. REQUIRED (asked if interactive).",
            ),
            click.option(
                "--purpose",
                "purposes",
                multiple=True,
                type=click.Choice(OFFERED_PURPOSES),
                help="What it may be used for. Repeatable. REQUIRED (asked if interactive).",
            ),
            click.option(
                "--model-class",
                "model_classes",
                multiple=True,
                type=click.Choice(OFFERED_MODEL_CLASSES),
                help="Who may use it. Repeatable. REQUIRED (asked if interactive).",
            ),
            click.option(
                "--people",
                type=click.Choice(PEOPLE_PRESENT),
                default=None,
                help="REQUIRED. Who identifiably appears: onlyMe, allConsented (everyone shown "
                "agreed to this use), or noIdentifiablePeople (excludes face/voice).",
            ),
            click.option(
                "--attest-adult",
                "adult",
                is_flag=True,
                help="REQUIRED. You affirm, signed by your key, that you are an adult able to consent.",
            ),
            click.option(
                "--expires-at",
                default=None,
                help="Optional expiry: ISO datetime, date, or duration (90d, 12w, 2y).",
            ),
            click.option(
                "--compensation",
                type=click.Choice(COMPENSATION),
                default="none",
                show_default=True,
                help="What you expect in return. A declaration only: ConsentVid does not pay or enforce it.",
            ),
            click.option(
                "--rights-basis",
                type=click.Choice(RIGHTS_BASIS),
                default="ownRecording",
                show_default=True,
                help="Why you may grant consent.",
            ),
            click.option(
                "--capture-provenance",
                type=click.Choice(CAPTURE_PROVENANCE),
                default="selfAttested",
                show_default=True,
                callback=_refuse_attested,
                help="'attested' is reserved for capture-time cryptographic provenance, which "
                "ConsentVid cannot verify yet, so it is refused.",
            ),
        ]
    ):
        f = opt(f)
    return f


def _validate_option(f):
    return click.option(
        "--validate/--no-validate",
        "validate",
        default=None,
        help="Force (or disable) PDS-side lexicon validation. --validate fails on any "
        "reference-implementation PDS today (none resolve third-party lexicons yet); leave it unset. "
        "Records are always validated locally first.",
    )(f)


# ---------------------------------------------------- register / attach ------


def _print_consent(uri, cid, record, replaced=False):
    from consentvid.policy import describe_terms

    click.secho(("Replaced" if replaced else "Created") + " consent receipt:", fg="green")
    click.echo(f"  uri:   {uri}")
    click.echo(f"  cid:   {cid}")
    click.echo(f"  terms: {describe_terms(record)}")
    click.echo("  Withdraw any time with: consentvid revoke " + uri)


# ---------------------------------------------------------------- CLI --------


@click.group(cls=_CLI, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="consentvid")
def main():
    """ConsentVid: register videos and signed consent receipts on ATproto, and
    build datasets that honour them."""
    _core.set_reporter(_click_reporter)
    _core._load_env()


@main.command()
def login():
    """Log in with an app password and cache the session (~/.consentvid).

    Your PDS is found from your handle (handle -> DID -> DID document), so
    the password only goes to the server that hosts your account.
    CONSENTVID_SERVICE overrides that."""
    handle = os.environ.get("CONSENTVID_HANDLE") or click.prompt("Handle or DID (e.g. you.bsky.social)")
    pw = os.environ.get("CONSENTVID_APP_PASSWORD") or click.prompt(
        "App password (bsky.app/settings/app-passwords)", hide_input=True
    )
    # A new login replaces the cached session: end the old one on its server
    # first, or it stays live with nothing left to end it (R3-13).
    if _core._end_cached_session():
        click.echo("Ended the previous session.")
    _core.SESSION_PATH.unlink(missing_ok=True)
    client = _core._session_client(handle, pw)
    click.echo(f"Logged in as {_safe(client.me.handle)} ({client.me.did})")


@main.command()
def logout():
    """End the session on the server and delete the cached copy."""
    if not _core.SESSION_PATH.exists():
        click.echo("No cached session.")
        return
    try:
        # deleteSession authenticates with the REFRESH token and invalidates it.
        _core._end_cached_session(strict=True)
        click.echo("Server session ended.")
    except Exception as e:  # noqa: BLE001 — best effort: always remove the local copy
        _warn(f"could not end the session on the server ({type(e).__name__}: {e}); it expires on its own")
    finally:
        _core.SESSION_PATH.unlink(missing_ok=True)
    click.echo("Logged out.")
    click.echo(
        "If you think the session or app password leaked, revoke the app password at "
        "https://bsky.app/settings/app-passwords"
    )


@main.command()
def whoami():
    """Show the logged-in identity."""
    client = _core.get_client()
    _check_session_scope(client, warn=True)
    click.echo(f"{_safe(client.me.handle)} ({client.me.did})")


def _session():
    """The authenticated API session the contributor commands act through."""
    from consentvid.api import Session

    return Session(_core.get_client())


def _confirm_strict_validation(validate) -> None:
    """--validate would be refused by the PDS only after the bytes are
    uploaded: say so first, and go on only if the user insists."""
    if _validate_flag(validate) is not True:
        return
    _warn(
        "--validate (or CONSENTVID_VALIDATE) will likely be rejected by your PDS after the upload "
        "('Unknown lexicon type': no PDS resolves third-party lexicons yet); the bytes would then be cleaned up"
    )
    if not _interactive() or not click.confirm("Continue with strict PDS validation?", default=False):
        raise ConsentVidError("not uploading: drop --validate / unset CONSENTVID_VALIDATE")


_CONSENT_KEYS = (
    "attest",
    "people",
    "adult",
    "modalities",
    "purposes",
    "model_classes",
    "expires_at",
    "compensation",
    "rights_basis",
    "capture_provenance",
)


def _terms_from(terms: dict):
    """The API's Terms from the CLI's (already prompted-for) options."""
    from consentvid.api import Terms

    return Terms(
        purposes=terms["purposes"],
        modalities=terms["modalities"],
        model_classes=terms["model_classes"],
        people=terms["people"],
        adult=bool(terms["adult"]),
        authorship=bool(terms["attest"]),
        expires=terms.get("expires_at"),
        compensation=terms["compensation"],
        rights_basis=terms["rights_basis"],
        capture_provenance=terms["capture_provenance"],
    )


def _api_video_opts(opts: dict) -> dict:
    out = dict(opts)
    out["storage"] = out.pop("storage_kind", None)
    return out


@main.command("register-video")
@click.argument("video_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@_video_options
@_validate_option
def register_video(video_path, validate, **video_opts):
    """Register a video: strip identifying metadata, hash, upload, and write a
    signed org.consentvid.video record. Then use attach-consent (or use
    `contribute` to do both in one step)."""
    if not video_opts.get("title"):
        raise click.UsageError("--title is required")
    _confirm_strict_validation(validate)
    video = _session().register_video(video_path, validate=validate, **_api_video_opts(video_opts))
    click.secho("Created video record:", fg="green")
    click.echo(f"  uri:  {video.uri}\n  cid:  {video.cid}\n  hash: {video.content_hash}")
    click.echo(f"Next: consentvid attach-consent {video.uri} --attest-authorship --attest-adult ...")


@main.command("attach-consent")
@click.argument("video_uri")
@_consent_options
@click.option("--replace", is_flag=True, help="Replace this video's existing consent terms.")
@_validate_option
def attach_consent(video_uri, replace, validate, **terms):
    """Attach a signed consent receipt to one of your videos (one per video)."""
    _require_terms(terms)
    consent = _session().attach_consent(video_uri, _terms_from(terms), replace=replace, validate=validate)
    _print_consent(consent.uri, consent.cid, consent.record, consent.replaced)


@main.command()
@click.argument("video_path", required=False, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--from-post",
    "from_post",
    default=None,
    metavar="POST",
    help="Contribute the video of one of YOUR Bluesky posts instead of a file: an at:// post URI "
    "or a https://bsky.app/profile/<you>/post/<id> link. The bytes stay on your PDS, tied to the "
    "post: deleting the post removes them from every dataset.",
)
@_video_options
@_consent_options
@_validate_option
def contribute(video_path, from_post, validate, **opts):
    """Register a video AND attach consent in one step (the usual workflow).

    Both records are written in one atomic commit: either both exist or
    neither does.

    \b
    Example:
      consentvid contribute clip.mp4 --title "Making pasta" --tag cooking \\
          --attest-authorship --attest-adult --people onlyMe \\
          --modality video --modality audio --purpose training --model-class academic
      consentvid contribute --from-post https://bsky.app/profile/me.bsky.social/post/3abc... \\
          --attest-authorship --attest-adult --people onlyMe \\
          --modality video --purpose training --model-class academic

    Any term you leave out is asked for when run interactively; nothing is
    granted by default. For many videos, use the Python API
    (Session.contribute_many; see docs/python-api.md).
    """
    if bool(video_path) == bool(from_post):
        raise click.UsageError("give either a VIDEO_PATH or --from-post, not both")
    if not from_post and not opts.get("title"):
        raise click.UsageError("--title is required")
    if from_post and (opts.get("storage_kind") or opts.get("storage_value")):
        raise click.UsageError("--storage-kind/--storage-value don't apply to --from-post: the bytes are the post's")
    terms = {k: opts.pop(k) for k in _CONSENT_KEYS}
    _require_terms(terms)  # fail before uploading anything
    api_terms = _terms_from(terms)
    _confirm_strict_validation(validate)
    session = _session()
    if from_post:
        for k in ("storage_kind", "storage_value"):
            opts.pop(k, None)
        c = session.contribute_from_post(from_post, api_terms, validate=validate, **opts)
    else:
        c = session.contribute(video_path, api_terms, validate=validate, **_api_video_opts(opts))
    click.secho("Created video record:", fg="green")
    click.echo(f"  uri:  {c.video_uri}\n  hash: {c.content_hash}")
    _print_consent(c.consent_uri, c.consent_cid, c.consent)


@main.command("list")
@click.option("--videos", "what", flag_value="videos", help="Only videos.")
@click.option("--consents", "what", flag_value="consents", help="Only consents.")
@click.option("--json", "as_json", is_flag=True, help="Full records as JSON.")
def list_records(what, as_json):
    """List your videos and consent receipts."""
    from consentvid.policy import describe_terms

    client = _core.get_client()
    did = client.me.did
    videos = [] if what == "consents" else _list_records(client, did, VIDEO_NSIDS)
    consents = [] if what == "videos" else _list_records(client, did, CONSENT_NSIDS)
    if as_json:
        dump = lambda rs: [{"uri": r.uri, "cid": r.cid, "value": r.raw} for r in rs]  # noqa: E731
        click.echo(json.dumps({"videos": dump(videos), "consents": dump(consents)}, indent=2))
        return
    by_video: dict[str, list] = {}
    for r in consents:
        by_video.setdefault(r.view.get("videoUri"), []).append(r)
    for r in videos:
        v = r.view
        ref = v.get("storageRef", {})
        click.secho(_safe(repr(v.get("title"))), bold=True)
        click.echo(f"  video:   {_safe(r.uri)}\n  storage: {_safe(ref.get('kind'))}  {_safe(ref.get('value'))}")
        linked = by_video.pop(r.uri, [])
        if not linked:
            click.secho("  consent: none", fg="yellow")
        if r.nsid == VIDEO_NSID:
            click.echo("  format:  version 1 (org.consentvid.video)")
        for c in linked:
            cv = c.view
            state = " (EXPIRED)" if is_expired(cv) else ""
            if c.uri.rsplit("/", 1)[-1] != r.uri.rsplit("/", 1)[-1]:
                state += " (ignored: not keyed to its video)"
            click.echo(f"  consent: {_safe(c.uri)}{state}\n           {_safe(describe_terms(cv))}")
    for vuri, cs in by_video.items():
        for c in cs:
            click.secho(f"consent {_safe(c.uri)} -> missing video {_safe(vuri)}", fg="yellow")
    if not videos and not consents:
        click.echo("Nothing registered yet. Try: consentvid contribute --help")


def _show_revoke_plan(video: dict, linked: list, delete_bytes: bool) -> None:
    ref = video.get("storageRef", {})
    click.echo(f"Video {_safe(repr(video.get('title')))} ({_safe(ref.get('kind'))}: {_safe(ref.get('value'))})")
    click.echo(f"  {len(linked)} consent receipt(s) will be deleted with it, in one commit.")
    if delete_bytes:
        click.echo("  The stored bytes will be deleted too.")


def _confirm_revoke(plan) -> bool:
    """The CLI's confirmation for a revoke plan (click.Abort on no)."""
    if plan.kind == "consent":
        click.confirm(f"Withdraw consent {plan.uri}?", abort=True)
        return True
    _show_revoke_plan(plan.video, plan.consents, plan.delete_bytes)
    click.confirm("Proceed?", abort=True)
    return True


@main.command()
@click.argument("uri")
@click.option(
    "--delete-bytes", is_flag=True, help="Also delete the video bytes from R2/PeerTube or unpin from IPFS."
)
@click.option("--all", "all_for_video", is_flag=True, hidden=True, help="Deprecated: no effect.")
@click.option("-y", "--yes", is_flag=True, help="Don't ask for confirmation.")
def revoke(uri, delete_bytes, all_for_video, yes):
    """Withdraw consent: delete a consent receipt, or a video together with all
    its consents (one atomic commit).

    Datasets learn about it when they run `consentvid resync`, and the AppView
    sees the deletion on the firehose immediately.
    """
    # Argument errors before any login.
    try:
        at = parse_at_uri(uri)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="URI") from None
    if at.collection not in RECORD_NSIDS:
        raise ConsentVidError(f"not a ConsentVid record: {at.collection}")
    if at.collection in CONSENT_NSIDS and delete_bytes:
        raise click.UsageError("--delete-bytes applies when revoking a video URI")
    if all_for_video:
        _warn("--all is deprecated and has no effect: a video has at most one consent")
    if not yes:
        # A plain video revoke shows its plan before asking; with --yes the
        # plan is still shown, as before.
        _session().revoke(uri, delete_bytes=delete_bytes, confirm=_confirm_revoke)
    else:

        def show(plan) -> bool:
            if plan.kind == "video":
                _show_revoke_plan(plan.video, plan.consents, plan.delete_bytes)
            return True

        _session().revoke(uri, delete_bytes=delete_bytes, confirm=show)


# ------------------------------------------------------------- datasets ------


def _own_did() -> str:
    handle = os.environ.get("CONSENTVID_HANDLE")
    if handle:
        from consentvid.identity import resolve_did

        return resolve_did(handle)
    return _core.get_client().me.did


def _both_spellings(values) -> tuple:
    """The internal values plus their V2 (kebab-case) spellings."""
    return tuple(dict.fromkeys([*values, *(versions.camel_to_kebab(v) for v in values)]))


def _camel_values(ctx, param, value):
    """Filters take either spelling of a value (V2 'open-weights' or
    'openWeights'); records are compared in the internal (camelCase) one."""
    return tuple(versions.kebab_to_camel(v) for v in value)


def _filter_options(f):
    for opt in reversed(
        [
            click.option(
                "--purpose",
                "purposes",
                multiple=True,
                type=click.Choice(PURPOSES),
                callback=_camel_values,
                help="Require this purpose. Repeatable (all required).",
            ),
            click.option(
                "--model-class",
                "model_classes",
                multiple=True,
                type=click.Choice(_both_spellings(MODEL_CLASSES)),
                callback=_camel_values,
                help="Require this model class (open-weights or openWeights). Repeatable (all required).",
            ),
            click.option(
                "--modality",
                "modalities",
                multiple=True,
                type=click.Choice(MODALITIES),
                help="Require this modality. Repeatable (all required).",
            ),
        ]
    ):
        f = opt(f)
    return f


def _include_labeled_option(f):
    return click.option(
        "--include-labeled",
        is_flag=True,
        help="Keep clips the contributor self-labeled porn, sexual, nudity or graphic-media "
        "(left out by default).",
    )(f)


@main.command()
@click.argument("output_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--repo", default=None, help="Contributor handle or DID (default: you). No login needed for others."
)
@_filter_options
@click.option("--tag", "tags", multiple=True, help="Only videos with any of these tags.")
@_include_labeled_option
@click.option("--name", default=None, help="Dataset name.")
@click.option("--description", default=None)
def export(output_path, repo, purposes, model_classes, modalities, tags, include_labeled, name, description):
    """Write a Croissant-RAI manifest of one contributor's consented clips
    (both record versions)."""
    from consentvid import signals
    from consentvid.croissant import Clip, build_manifest
    from consentvid.identity import display_name, public_client, resolve_did
    from consentvid.policy import covers

    did = resolve_did(repo) if repo else _own_did()
    client = public_client(did)
    try:
        account = signals.account_signals(did)
    except Exception as e:  # noqa: BLE001 — fail closed
        raise ConsentVidError(
            f"could not check the contributor's bot label and AI-usage preferences ({type(e).__name__}: {_safe(e)})"
        ) from e
    if account.bot:
        raise ConsentVidError(f"{_safe(did)} labels itself a bot account: bots cannot give consent")
    videos = {r.uri: r for r in _list_records(client, did, VIDEO_NSIDS)}
    # Each video's consent is its sidecar: the consent with the video's own
    # record key, of the same record version. Any other consent is ignored.
    best: dict[str, tuple[str, dict]] = {}
    cids: dict[str, str] = {}
    for r in _list_records(client, did, CONSENT_NSIDS):
        c = r.view
        if not isinstance(c.get("videoUri"), str):
            _warn(f"skipping malformed consent {_safe(r.uri)}")
            continue
        try:
            cat = parse_at_uri(r.uri, r.nsid)
            vat = parse_at_uri(c["videoUri"], VIDEO_FOR_CONSENT[r.nsid])
        except ValueError as e:
            _warn(f"skipping consent {_safe(r.uri)}: {_safe(e)}")
            continue
        if cat.did != did or vat.rkey != cat.rkey:
            _warn(f"ignored consent {_safe(r.uri)}: record key doesn't match its video")
            continue
        errors = validate_in_repo(r.nsid, r.raw, did, rkey=cat.rkey)
        if errors:
            _warn(f"leaving out {_safe(c['videoUri'])}: its consent {_safe(r.uri)} is invalid ({_safe(errors[0])})")
            continue
        if is_expired(c):
            _warn(f"skipping expired consent {_safe(r.uri)}")
            continue
        conflict = account.conflicts(c)
        if conflict:
            _warn(f"leaving out {_safe(c['videoUri'])}: {_safe(conflict)}")
            continue
        best[c["videoUri"]] = (r.uri, c)
        cids[r.uri] = r.cid

    handle = display_name(did)
    clips = []
    for vuri, (curi, c) in best.items():
        if vuri not in videos:
            _warn(f"consent {_safe(curi)} references missing video {_safe(vuri)}")
            continue
        vr = videos[vuri]
        vcid, v = vr.cid, vr.view
        if validate_in_repo(vr.nsid, vr.raw, did, rkey=vr.rkey):
            _warn(f"skipping {_safe(curi)}: its video record is invalid")
            continue
        if c["videoCid"] != vcid:  # validate_in_repo requires videoCid
            _warn(f"skipping {_safe(curi)}: video record changed after consent was given")
            continue
        if not covers(c, purposes=purposes, model_classes=model_classes, modalities=modalities):
            continue
        if tags and not set(tags) & set(v.get("tags") or []):
            continue
        sensitive = set(v.get("labels") or []) & set(SENSITIVE_SELF_LABELS)
        if sensitive and not include_labeled:
            _warn(f"left out {_safe(vuri)}: self-labeled {', '.join(sorted(sensitive))} (--include-labeled keeps it)")
            continue
        ref = v.get("storageRef") or {}
        if ref.get("kind") == versions.BSKY_KIND:
            from consentvid import bsky
            from consentvid.verify import fetch_verified

            try:
                problem = bsky.check_post(ref, did, fetch_verified)
            except Exception as e:  # noqa: BLE001 — fail closed: leave it out
                problem = ("unknown", f"couldn't check its Bluesky post ({type(e).__name__}: {_safe(e)})")
            if problem:
                _warn(f"left out {_safe(vuri)}: {_safe(problem[1])}")
                continue
        clips.append(Clip(curi, cids[curi], c, vuri, vcid, v, did, handle))

    name = name or f"consentvid-{handle.replace('.', '-').replace(':', '-')}-{date.today()}"
    manifest, skipped = build_manifest(clips, name=name, description=description)
    for clip in skipped:
        _warn(
            f"left out {_safe(repr(clip.video.get('title')))}: its bytes aren't publicly reachable "
            f"(storage kind {_safe(clip.video.get('storageRef', {}).get('kind'))})"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2) + "\n")
    click.secho(f"Wrote {output_path}: {len(manifest['distribution'])} clip(s) from {_safe(handle)}", fg="green")


@main.command()
@click.argument("uri")
@click.option("--json", "as_json", is_flag=True)
@click.option(
    "--no-relay-status",
    "no_relay_status",
    is_flag=True,
    help="Don't ask the relay (getRepoStatus) whether the contributor's account is active, e.g. "
    "offline. By default a relay-level takedown makes a clip unavailable.",
)
def verify(uri, as_json, no_relay_status):
    """Cryptographically verify a record against its author's repo (no login).

    Checks the commit signature against the DID's key and the Merkle proof of
    the record's presence or absence, then validates the record.
    """
    from consentvid.resync import stale_against, witness_rev_of
    from consentvid.verify import fetch_verified

    try:
        at = parse_at_uri(uri)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="URI") from None
    # The relay's witness first (see resync: STALE), then the PDS.
    witnessed = None if no_relay_status else witness_rev_of(at.did)
    rec = fetch_verified(at)
    errors = validate_in_repo(at.collection, rec.value, at.did, rkey=at.rkey) if rec.present else []
    stale = stale_against(witnessed, rec)
    post_problem = None
    if rec.present and not errors and at.collection == VIDEO_V2_NSID:
        ref = versions.video_view(VIDEO_V2_NSID, rec.value, at.did).get("storageRef") or {}
        if ref.get("kind") == versions.BSKY_KIND:
            from consentvid import bsky

            try:
                problem = bsky.check_post(ref, at.did, fetch_verified)
            except Exception as e:  # noqa: BLE001 — reported, not fatal
                problem = ("unknown", f"couldn't check the Bluesky post: {type(e).__name__}: {e}")
            post_problem = problem[1] if problem else None
    relay = relay_lag = None
    if rec.present and not no_relay_status:
        from consentvid.appview import is_operational
        from consentvid.resync import relay_status_of

        answer = relay_status_of(at.did)
        if is_operational(answer):
            relay_lag = answer[1]  # the relay's own processing state, not the account's (R3-01)
        elif answer is not None and not answer[0]:
            relay = answer[1] or "inactive"
    if as_json:
        click.echo(
            json.dumps(
                {
                    "uri": uri,
                    "present": rec.present,
                    "cid": rec.cid,
                    "rev": rec.rev,
                    "proven": rec.proven,
                    "errors": errors,
                    "relayStatus": relay,
                    "relayProcessingStatus": relay_lag,
                    "relayWitnessedRev": witnessed,
                    "stale": stale,
                    "postProblem": post_problem,
                    "value": rec.value,
                },
                indent=2,
            )
        )
    elif not rec.present:
        how = "proven absent by a signed repo commit" if rec.proven else "reported absent by the PDS"
        click.secho(f"ABSENT — {how} (rev {_safe(rec.rev or '?')})", fg="red")
        if stale:
            click.secho(f"  STALE: the relay has seen rev {_safe(witnessed)}; the PDS may be rolled back", fg="red")
    else:
        click.secho("PRESENT — signature and Merkle proof verified", fg="green")
        click.echo(f"  cid: {_safe(rec.cid)}\n  rev: {_safe(rec.rev)}")
        if errors:
            click.secho("  INVALID: " + "; ".join(_safe(e) for e in errors), fg="red")
        if stale:
            click.secho(
                f"  STALE: the relay has seen rev {_safe(witnessed)}, newer than this commit: "
                "the PDS served a rolled-back or stale repo",
                fg="red",
            )
        if post_problem:
            click.secho(f"  BYTES: {_safe(post_problem)}", fg="red")
        if relay:
            click.secho(f"  UNAVAILABLE: the relay reports the account {_safe(relay)}", fg="red")
        elif relay_lag:
            click.secho(f"  note: relay reports {_safe(relay_lag)}; live indexes may be stale", fg="yellow")
        click.echo(json.dumps(rec.value, indent=2, default=str))
    if not rec.present or errors or relay or stale or post_problem:
        sys.exit(1)


_MARK = {
    "valid": ("ok", "green"),
    "revoked": ("REVOKED", "red"),
    "expired": ("EXPIRED", "red"),
    "modified": ("MODIFIED", "red"),
    "invalid": ("INVALID", "red"),
    "unattested": ("UNATTEST", "red"),
    "unavailable": ("UNAVAIL", "red"),
    "unbound": ("UNBOUND", "red"),
    "bot-account": ("BOT", "red"),
    "conflicting-preference": ("PREF", "red"),
    "labeled": ("LABELED", "red"),
    "stale": ("STALE", "red"),
    "unknown": ("?", "yellow"),
}


@main.command()
@click.argument("input_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--write",
    "write_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write a cleaned manifest without the clips that must be dropped.",
)
@click.option(
    "--min-provenance",
    type=click.Choice(CAPTURE_PROVENANCE),
    help="Also drop clips whose capture provenance is below this tier. 'attested' is not "
    "verifiable yet (a signed 'attested' counts as selfAttested), so it drops every clip.",
)
@click.option(
    "--strict/--lenient",
    default=True,
    show_default=True,
    help="--strict (default) drops clips that couldn't be checked (network errors, PDS 5xx); "
    "--lenient keeps them as they are, unverified. Whoever runs a contributor's PDS can make "
    "its clips uncheckable, so only use --lenient on manifests you built yourself.",
)
@click.option("--check", is_flag=True, help="Exit with status 1 if any clip must be dropped.")
@click.option("--json", "as_json", is_flag=True)
@click.option("--workers", default=8, show_default=True, help="Parallel checks.")
@_include_labeled_option
@click.option(
    "--no-relay-status",
    "no_relay_status",
    is_flag=True,
    help="Don't ask the relay (getRepoStatus) whether the contributor's account is active, e.g. "
    "offline. By default a relay-level takedown makes a clip unavailable.",
)
def resync(input_path, write_path, min_provenance, strict, check, as_json, workers, include_labeled, no_relay_status):
    """Verify that every clip's consent still stands, before you use a dataset.

    INPUT_PATH is a Croissant manifest or a C2PA-stamped video file. Each
    consent and video is verified against the contributor's repo with signature
    and Merkle proofs. Run this before every training run. No login needed.

    A C2PA file is reported UNBOUND at best: its embedded receipt points at the
    consent, but nothing binds that file's bytes to it. Use the registered
    bytes (same sha256) instead. --write rebuilds every kept manifest entry
    from the verified records.
    """
    from consentvid import resync as rs

    try:
        manifest = rs.load_manifest_json(input_path.read_bytes())
    except ConsentVidError:
        raise  # duplicate keys / nesting: a manifest, but not one we can trust
    except (UnicodeDecodeError, ValueError):
        manifest = None
    if isinstance(manifest, dict):
        items = rs.items_from_manifest(manifest)
    else:
        manifest = None
        from consentvid import c2pa_embed as ce

        try:
            receipt, c2pa_state = ce.extract_consent_receipt(input_path)
        except ConfigError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ConsentVidError(
                f"{input_path} is neither a manifest nor a readable C2PA file: {_safe(e)}"
            ) from e
        if not receipt:
            raise ConsentVidError("embedded C2PA manifest has no org.consentvid.consent assertion")
        items = [rs.item_from_c2pa(receipt, input_path.name, c2pa_state)]
    if not items:
        raise ConsentVidError("no clips with consent receipts found")

    results = rs.check_all(
        items,
        min_provenance=min_provenance,
        workers=workers,
        relay_status=not no_relay_status,
        include_labeled=include_labeled,
    )
    drop_statuses = rs.DROP_STATUSES + ((rs.UNKNOWN,) if strict else ())
    n_drop = sum(r.status in drop_statuses for r in results)
    if as_json:
        click.echo(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        for r in results:
            label, color = _MARK[r.status]
            click.secho(f"  [{label:>8}] ", fg=color, nl=False)
            # Names and reasons come from the manifest and the PDS: untrusted.
            click.echo(_safe(r.name) + (f"  — {_safe(r.reason)}" if r.reason else ""))
        counts = {s: sum(r.status == s for r in results) for s in rs.STATUSES}
        reset = click.style("", reset=True)
        click.echo(reset + "\n" + ", ".join(f"{s}: {n}" for s, n in counts.items() if n))
        click.echo(reset + f"{n_drop} of {len(results)} clip(s) must be dropped before use.")
        if counts[rs.UNKNOWN] and not strict:
            _warn("some clips couldn't be checked (network); --lenient keeps them. Re-run to check again.")
        if manifest is not None and not write_path:
            click.echo(
                "Note: dataset-level text (description, rai:*, creator) is not verified; use --write "
                "to get a manifest rebuilt from signed records only."
            )
    if write_path:
        if manifest is None:
            raise click.UsageError("--write needs a manifest as input")
        cleaned = rs.filter_manifest(manifest, results, now_iso(), strict=strict)
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(json.dumps(cleaned, indent=2) + "\n")
        if not as_json:
            click.echo(f"Wrote {write_path} ({len(cleaned['distribution'])} clip(s)).")
    if check and n_drop:
        sys.exit(1)


@main.command("embed-c2pa")
@click.argument("video_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--consent", "consent_uri", required=True, help="AT-URI of the consent receipt.")
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Default: <video>.c2pa.<ext>. An existing file (or symlink) there is refused, never overwritten.",
)
def embed_c2pa(video_path, consent_uri, output_path):
    """Stamp a signed C2PA manifest carrying the consent receipt into a copy of
    the video, so a pointer to the consent survives the file leaving the
    platform. It is a pointer, not proof: resync reports stamped files as
    UNBOUND (the stamped bytes differ from the registered ones).

    The file must be exactly the registered bytes. Needs the [c2pa] extra.
    """
    from consentvid import c2pa_embed as ce
    from consentvid.identity import verified_handle
    from consentvid.resync import parse_consent_uri
    from consentvid.verify import fetch_verified

    try:
        consent_at = parse_consent_uri(consent_uri)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--consent") from None
    consent = fetch_verified(consent_at)
    if not consent.present:
        raise ConsentVidError(f"{consent_uri} does not exist (withdrawn?)")
    errors = validate_in_repo(consent_at.collection, consent.value, consent_at.did, rkey=consent_at.rkey)
    if errors:
        raise ConsentVidError(f"consent is invalid: {errors[0]}")
    c = versions.consent_view(consent_at.collection, consent.value)
    video_uri = c["videoUri"]
    video_nsid = VIDEO_FOR_CONSENT[consent_at.collection]
    video_at = parse_at_uri(video_uri, video_nsid)
    video = fetch_verified(video_at)
    if not video.present:
        raise ConsentVidError(f"the consent's video {video_uri} no longer exists")
    errors = validate_in_repo(video_nsid, video.value, video_at.did, rkey=video_at.rkey)
    if errors:
        raise ConsentVidError(f"the consent's video record is invalid: {errors[0]}")
    v = versions.video_view(video_nsid, video.value, video_at.did)
    if c["videoCid"] != video.cid:  # validate_in_repo requires videoCid
        raise ConsentVidError("the video record changed after consent was given; consent no longer applies")
    actual = hash_file(video_path)
    if actual != v.get("contentHash"):
        raise ConsentVidError(
            f"{video_path.name} is not the registered video\n"
            f"  file:   {actual}\n  record: {v.get('contentHash')}"
        )
    output_path = output_path or video_path.with_suffix(f".c2pa{video_path.suffix}")
    out = ce.embed_consent(
        video_path=video_path,
        out_path=output_path,
        consent_uri=consent_uri,
        consent_cid=consent.cid,
        consent=c,
        video_uri=video_uri,
        video_cid=video.cid,
        video=v,
        contributor_handle=_verified_handle(consent_at.did, lambda d: verified_handle(d, raise_errors=True)),
        contributor_did=consent_at.did,
    )
    click.secho(f"Embedded C2PA consent manifest -> {out}", fg="green")
    _warn(
        "signed with a self-signed prototype certificate; C2PA readers will mark the "
        "signer untrusted. The stamped file's hash differs from the registered one, so "
        "`consentvid resync` reports it UNBOUND: the receipt points at the consent but does "
        "not prove this file is the consented bytes. Share the registered bytes for training."
    )


def _verified_handle(did: str, verify_handle) -> str | None:
    """The DID's bidirectionally verified handle (``verify_handle`` is
    identity.verified_handle), or None. Never the DID or an unverified
    claim: a handle field must not carry something that isn't a handle."""
    from consentvid.identity import HandleLookupError

    try:
        handle = verify_handle(did)
    except HandleLookupError as e:
        _warn(f"handle not included: lookup failed ({_safe(e)})")
        return None
    except Exception:  # noqa: BLE001 — no verified handle
        return None
    return handle if isinstance(handle, str) else None


@main.command("read-c2pa")
@click.argument("video_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def read_c2pa(video_path):
    """Show the consent receipt embedded in a C2PA-stamped file."""
    from consentvid import c2pa_embed as ce

    try:
        man = ce.read_manifest(video_path)
    except ConfigError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ConsentVidError(f"no readable C2PA manifest in {video_path.name}: {_safe(e)}") from e
    active = man.get("manifests", {}).get(man.get("active_manifest"), {})
    click.echo(f"validation_state: {_safe(man.get('validation_state'))}")
    click.echo(f"title:            {_safe(active.get('title'))}")
    receipt = next(
        (a.get("data") for a in active.get("assertions", []) if a.get("label") == ce.CONSENT_ASSERTION_LABEL),
        None,
    )
    if receipt is None:
        click.echo("No org.consentvid.consent assertion present.")
        return
    click.echo(json.dumps(receipt, indent=2))
    click.echo(f"\nCheck it is still valid: consentvid resync {video_path}")


# ------------------------------------------------------------- lexicons ------


@main.command("publish-lexicons")
@click.option("--nsid", "nsids", multiple=True, help="Only these NSIDs (default: all).")
@click.option(
    "--bootstrap",
    is_flag=True,
    help="Allow publishing although no _lexicon DNS record names an authority yet (first publication).",
)
@click.option(
    "--allow-breaking",
    is_flag=True,
    help="Skip the lexicon-evolution check. Only for a brand-new NSID: a published lexicon may "
    "only gain optional fields and descriptions.",
)
@click.option(
    "--skip-pds-validation",
    is_flag=True,
    help="Send validate=false (escape hatch if your PDS rejects the com.atproto.lexicon.schema record).",
)
def publish_lexicons(nsids, bootstrap, allow_breaking, skip_pds_validation):
    """Publish the lexicons as com.atproto.lexicon.schema records in your repo.

    Only the lexicon authority may publish: the account named by the
    _lexicon.<authority> DNS TXT record. Each change is checked against the
    published version with the lexicon evolution rules first (also with
    --bootstrap, against whatever your repo already holds), and written with
    compare-and-swap: a schema that changed meanwhile is never overwritten."""
    from consentvid import lexicons as lx
    from consentvid.identity import public_client

    client = _core.get_client()
    me = client.me.did
    targets = list(nsids) or lx.discover_lexicons()
    plans = []
    for nsid in targets:
        try:
            auth = lx.resolve_did_via_dns(nsid, strict=True)
        except lx.ConflictingLexiconRecords as e:
            raise ConsentVidError(str(e)) from None
        if auth is None and not bootstrap:
            raise ConsentVidError(
                f"no {lx.dns_record_name(nsid)} TXT record: pass --bootstrap to publish the first version, "
                f'then add  {lx.dns_record_name(nsid)}  TXT  "did={me}"'
            )
        if auth is not None and auth != me:
            raise ConsentVidError(
                f"you are not the lexicon authority for {lx.nsid_authority(nsid)} "
                f"(DNS names {_safe(auth)}, you are {me}); refusing to publish unofficial schemas"
            )
        local = lx.load_lexicon(nsid)
        # Whatever is published already (with --bootstrap: in your own repo)
        # is checked against the evolution rules, and its CID guards the write.
        published = _get_record_or_none(public_client(auth or me), auth or me, lx.SCHEMA_COLLECTION, nsid)
        if published is not None and not allow_breaking:
            old = {k: v for k, v in to_dict(published.value).items() if k != "$type"}
            problems = lx.compat_errors(old, local)
            if problems:
                raise ConsentVidError(
                    f"{nsid}: the change breaks the lexicon evolution rules (use a new NSID instead):\n  - "
                    + "\n  - ".join(problems)
                )
        plans.append((nsid, lx.build_schema_record(local), str(published.cid) if published is not None else None))
    for nsid, record, existing_cid in plans:
        params = {"repo": me, "collection": lx.SCHEMA_COLLECTION, "rkey": nsid, "record": record}
        if skip_pds_validation:
            params["validate"] = False
        try:
            if existing_cid is None:
                # createRecord fails if the key appeared meanwhile.
                client.com.atproto.repo.create_record(params)
            else:
                client.com.atproto.repo.put_record({**params, "swapRecord": existing_cid})
        except Exception as e:
            text = f"{_xrpc_error_name(e)} {e}"
            if "InvalidSwap" in text or "already exists" in text.lower():
                raise ConsentVidError(
                    f"{nsid}: the published schema changed while you were publishing; re-run"
                ) from e
            if not isinstance(e, AtProtocolError):
                raise
            hint = "" if skip_pds_validation else " (if your PDS rejects valid lexicons, retry with --skip-pds-validation)"
            raise ConsentVidError(f"{nsid}: {_xrpc_message(e)}{hint}") from e
        click.echo(f"Published {nsid} -> at://{me}/{lx.SCHEMA_COLLECTION}/{nsid}")
    by_authority = {lx.nsid_authority(n): n for n in targets}
    for auth, nsid in sorted(by_authority.items()):
        try:
            points_at_me = lx.resolve_did_via_dns(nsid) == me
        except ConsentVidError as e:
            _warn(f"could not check _lexicon.{auth}: {_safe(e)}")
            continue
        if points_at_me:
            click.echo(f"DNS _lexicon.{auth} already points at you.")
        else:
            click.echo(f'Add DNS TXT:  _lexicon.{auth}  "did={me}"')


def _verified_lexicon(did: str, nsid: str) -> dict:
    """The schema record for ``nsid`` in ``did``'s repo, verified like any
    record (commit signature + MST proof), with the $type and id the lexicon
    spec requires. Raises ConsentVidError with a one-line reason."""
    from consentvid import lexicons as lx
    from consentvid.errors import RepoUnavailable, VerificationError
    from consentvid.verify import fetch_verified

    uri = f"at://{did}/{lx.SCHEMA_COLLECTION}/{nsid}"
    try:
        rec = fetch_verified(uri)
    except (VerificationError, RepoUnavailable) as e:
        raise ConsentVidError(f"{uri} could not be verified: {_safe(e)}") from e
    if not rec.present:
        raise ConsentVidError(f"{uri} does not exist")
    value = rec.value or {}
    if value.get("$type") != lx.SCHEMA_COLLECTION:
        raise ConsentVidError(f"{uri} has $type {_safe(value.get('$type'))!r}, not {lx.SCHEMA_COLLECTION}")
    if value.get("id") != nsid:
        raise ConsentVidError(f"{uri} declares id {_safe(value.get('id'))!r}, not {nsid}")
    return value


@main.command("resolve-lexicon")
@click.argument("nsid")
@click.option("--did", "did_override", default=None, help="Skip DNS; read from this DID.")
def resolve_lexicon(nsid, did_override):
    """Resolve a lexicon like a validator would (DNS -> DID -> schema record),
    verify the record against the authority's repo, and compare it with the
    local copy. No login needed."""
    from consentvid import lexicons as lx
    from consentvid.errors import LexiconDnsError

    try:
        did = did_override or lx.resolve_did_via_dns(nsid, strict=True)
    except lx.ConflictingLexiconRecords as e:
        raise ConsentVidError(f"{e}: resolution fails until only one remains") from None
    except LexiconDnsError as e:
        raise ConsentVidError(f"DNS error: {e}") from None
    if not did:
        raise ConsentVidError(f"no TXT record at {lx.dns_record_name(nsid)}")
    click.echo(f"{lx.dns_record_name(nsid)} -> {_safe(did)}")
    value = _verified_lexicon(did, nsid)
    click.echo(f"Resolved at://{_safe(did)}/{lx.SCHEMA_COLLECTION}/{nsid}: verified (commit signature + MST proof)")
    try:
        same = lx.schema_matches(lx.load_lexicon(nsid), value)
    except FileNotFoundError:
        click.echo("(no local copy to compare)")
        return
    if same:
        click.secho("Published schema matches the local lexicon.", fg="green")
    else:
        _warn("published schema differs from the local lexicon: run `consentvid publish-lexicons`")


# --------------------------------------------------------------- doctor ------


@main.command()
def doctor():
    """Check your setup: login, tools, storage backends, lexicons, AppView."""
    from consentvid import appview as av
    from consentvid import lexicons as lx
    from consentvid import media, storage
    from consentvid.errors import LexiconDnsError

    def line(ok, label, detail=""):
        mark, color = {True: ("ok", "green"), False: ("!!", "red"), None: ("--", "yellow")}[ok]
        click.secho(f"  [{mark}] ", fg=color, nl=False)
        click.echo(label + (f": {detail}" if detail else ""))

    click.echo(f"consentvid {__version__}, Python {sys.version.split()[0]}")
    line(sys.version_info < (3, 14), "Python < 3.14", "the atproto SDK breaks on 3.14")
    for env_file in _env_files():
        if env_file.is_file():
            too_open = _env_file_too_open(env_file)
            line(
                not too_open,
                f"settings file {env_file}",
                f"readable by other users: chmod 600 {env_file}" if too_open else "private (0600)",
            )
    try:
        client = _core.get_client()
        line(True, "logged in", f"{_safe(client.me.handle)} ({client.me.did})")
    except Exception as e:  # noqa: BLE001
        line(False, "logged in", _safe((str(e).splitlines() or [""])[0]))
    line(
        media.have_ffprobe() or None,
        "ffprobe",
        "duration, size, metadata check" if media.have_ffprobe() else "missing (brew install ffmpeg)",
    )
    line(
        media.have_ffmpeg() or None,
        "ffmpeg",
        "metadata stripping" if media.have_ffmpeg() else "missing: can't strip GPS/device tags",
    )
    for kind in storage.UPLOAD_KINDS:
        ok = storage.configured(kind)
        line(ok or None, f"storage {kind}", "configured" if ok else "not configured")
    click.echo(f"       default storage: {_default_storage()}")
    try:
        import c2pa  # noqa: F401

        line(True, "c2pa extra", "installed")
    except ImportError:
        line(None, "c2pa extra", 'not installed (pip install "consentvid[c2pa]")')
    for nsid in lx.discover_lexicons():
        try:
            try:
                did = lx.resolve_did_via_dns(nsid, strict=True)
            except lx.ConflictingLexiconRecords:
                line(False, f"lexicon {nsid}", f"conflicting _lexicon TXT records at {lx.dns_record_name(nsid)}")
                continue
            except LexiconDnsError as e:
                line(False, f"lexicon {nsid}", f"DNS error: {_safe(e)}")
                continue
            if not did:
                line(None, f"lexicon {nsid}", "no _lexicon DNS record")
                continue
            pub = _verified_lexicon(did, nsid)
            same = lx.schema_matches(lx.load_lexicon(nsid), pub)
            line(
                same,
                f"lexicon {nsid}",
                "published, verified, up to date" if same else "published copy is outdated: run publish-lexicons",
            )
        except Exception as e:  # noqa: BLE001
            line(False, f"lexicon {nsid}", _safe(str(e)[:100]))
    if av.db_path().exists():
        s = av.stats(av.connect())
        line(True, "appview index", f"{s['consents']} consents, {s['videos']} videos, cursor {s['cursor']}")
        if s.get("gap_since"):
            line(
                False,
                "appview gap",
                f"events after {s['gap_since']} may be missing; repair: consentvid appview backfill --all",
            )
        if s.get("pending_backfill"):
            line(
                None,
                "appview pending",
                f"{s['pending_backfill']} repo(s) queued for a re-read ({s.get('pending_retrying', 0)} retrying); "
                "`appview index` re-reads them, `consentvid appview backfill --pending` forces it",
            )
        if not s["consents"] and not s["videos"]:
            line(None, "appview empty", "fill it with: consentvid appview backfill --all")
    else:
        line(None, "appview index", "not created yet")


# -------------------------------------------------------------- appview ------


@main.group()
def appview():
    """Index ConsentVid records from the whole network (local SQLite)."""


def _relay_url(value: str | None) -> str:
    from consentvid import appview as av
    from consentvid.storage import require_https

    return require_https("--relay", value or os.environ.get("CONSENTVID_RELAY") or av.DEFAULT_RELAY).rstrip("/")


@appview.command("backfill")
@click.argument("repos", nargs=-1)
@click.option(
    "--all",
    "all_repos",
    is_flag=True,
    help="Every contributor: repos the relay lists (com.atproto.sync.listReposByCollection) plus every "
    "repo already in the index. Repairs a gap.",
)
@click.option("--pending", is_flag=True, help="Repos queued for a re-read (#sync, reactivated accounts).")
@click.option(
    "--relay",
    default=None,
    help="Relay for discovery and repo status. Default: $CONSENTVID_RELAY, else relay1.us-east.bsky.network.",
)
def appview_backfill(repos, all_repos, pending, relay):
    """Index existing records from contributors (handles or DIDs, --all, or --pending)."""
    from consentvid import appview as av
    from consentvid.records import is_did

    if not (repos or all_repos or pending):
        raise click.UsageError("name contributors (handles or DIDs), or pass --all or --pending")
    relay = _relay_url(relay)
    conn = av.connect()
    # A gap recorded while this run lists repos covers events after what it
    # already read: only the gap seen now may be cleared at the end (R2-07).
    gap_at_start = av.gap_token(conn)
    targets: list[str] = list(repos)
    if all_repos:
        listed: list[str] = []
        for nsid in RECORD_NSIDS:
            listed += av.list_repos_by_collection(relay, nsid)
        known = av.known_dids(conn)
        click.echo(f"Relay lists {len(set(listed))} repo(s); the index knows {len(known)}.")
        targets += sorted(set(listed) | known)
    if pending or all_repos:
        targets += av.pending_dids(conn)
    seen, failures, done = set(), 0, 0
    for repo in targets:
        key = repo if is_did(repo) else repo.lower().removeprefix("@")
        if key in seen:
            continue
        seen.add(key)
        try:
            # _backfill_repo settles the repo's pending row itself (cleared,
            # or kept while the relay lags: R3-01).
            did, stored, invalid = _backfill_repo(conn, repo, relay=relay)
        except Exception as e:  # noqa: BLE001 — one bad repo must not stop the others
            conn.rollback()
            failures += 1
            _warn(f"{_safe(repo)}: backfill failed ({type(e).__name__}: {_safe(e)}); continuing")
            continue
        done += 1
        click.echo(f"{_safe(repo)}: {stored} record(s) indexed, {invalid} skipped")
    if all_repos and not failures:
        if av.clear_gap_if(conn, gap_at_start):
            if gap_at_start[0] is not None:
                click.echo("Gap repaired.")
        else:
            _warn("a new gap was recorded during this run; run `consentvid appview backfill --all` again")
    click.echo(f"{done} repo(s) backfilled, {failures} failed.")
    if failures and av.get_gap(conn):
        _warn("the gap stays recorded until every repo backfills; re-run `consentvid appview backfill --all`")


def _repo_statuses(did: str, relay: str) -> list[tuple[bool, str | None] | None]:
    """getRepoStatus from the contributor's PDS and from the relay; None for
    a source that couldn't answer. RepoNotFound counts as "deleted" only when
    the other source doesn't vouch for the repo: a relay only knows the repos
    it crawls, and the PDS only those it hosts."""
    from consentvid import appview as av
    from consentvid.errors import RepoUnavailable
    from consentvid.identity import public_client, with_retry

    pds: tuple[bool, str | None] | str | None
    try:
        st = with_retry(lambda: public_client(did).com.atproto.sync.get_repo_status({"did": did}))
        pds = (bool(st.active), getattr(st, "status", None))
    except RepoUnavailable as e:
        # A tombstoned DID (plc.directory, a trust root) is deleted, whatever
        # a relay still remembers. Other identity failures say nothing.
        pds = (False, "deleted") if e.status == "deleted" else None
    except Exception as e:  # noqa: BLE001
        pds = "not-found" if _xrpc_error_name(e) == "RepoNotFound" else None
    rel: tuple[bool, str | None] | str | None
    try:
        rel = av.relay_repo_status(relay, did)
    except RepoUnavailable:
        rel = "not-found"
    except Exception:  # noqa: BLE001
        rel = None

    def resolve(mine, other):
        if mine != "not-found":
            return mine
        # A relay that is throttling a repo still knows it exists (R3-01).
        vouched = isinstance(other, tuple) and (other[0] or av.is_operational(other))
        return None if vouched or other is None else (False, "deleted")

    return [resolve(pds, rel), resolve(rel, pds)]


_STATUS_SEVERITY = ("deleted", "takendown", "suspended", "deactivated")


def _combine_status(answers) -> tuple[bool, str | None] | None:
    """The most restrictive answer: inactive if any source says so; active
    only if every source answered active; None if that can't be decided.
    A relay processing status (throttled, host-throttled, desynchronized) is
    not an account status and casts no vote (R3-01; see _relay_lag)."""
    from consentvid.appview import is_operational

    answers = [a for a in answers if not is_operational(a)]
    inactive = [a for a in answers if a is not None and not a[0]]
    if inactive:
        statuses = [a[1] for a in inactive]
        for s in _STATUS_SEVERITY:
            if s in statuses:
                return False, s
        return False, statuses[0]
    if answers and all(a is not None and a[0] for a in answers):
        return True, None
    return None


def _backfill_repo(conn, repo: str, relay: str | None = None) -> tuple[str, int, int]:
    """Re-read one repo into the index. Returns (did, stored, invalid).

    The repo status (PDS + relay getRepoStatus, or a tombstoned DID) is
    decided first: a deactivated, taken-down, suspended or deleted repo is
    not listed at all (its PDS refuses listRecords, and the sync spec says to
    treat it as unavailable). Its status is recorded and the backfill counts
    as done, so `--all` can clear a gap that hid its #account event (R2-01)."""
    from consentvid import appview as av
    from consentvid.identity import public_client, resolve_did

    did = resolve_did(repo)
    # Fence: anything the live indexer writes for this repo from now on is
    # newer than our listing, and must win over it (R3-07).
    # Timestamps have millisecond precision: start the fence on a fresh
    # millisecond, so writes made before this call are strictly older.
    before = now_iso()
    while (started := now_iso()) == before:
        time.sleep(0.0002)
    # Read everything BEFORE writing: the listing and the status checks are
    # network I/O against untrusted hosts and must not run while we hold the
    # database's write lock (the live indexer would time out behind us).
    answers = _repo_statuses(did, _relay_url(relay))
    status = _combine_status(answers)
    if status is not None and not status[0]:
        return _record_inactive_repo(conn, did, status[1], started)
    relay_lag = _relay_lag(answers)
    client = public_client(did)
    listings = {collection: _core._list_all(client, did, collection) for collection in RECORD_NSIDS}
    # Account signals (bot self-label, AI-usage preferences), read with
    # proofs; a failure fails the backfill, which is retried (fail closed:
    # the account's consents stay hidden until it succeeds).
    from consentvid import signals

    account = signals.account_signals(did, fresh=True)
    stored = invalid = skipped = 0
    # Take the write lock, then see what changed while we were listing.
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    touched_consents = {
        row["uri"]
        for row in conn.execute(
            "SELECT uri FROM consents WHERE did = ? AND "
            "max(indexed_at, COALESCE(revoked_at, ''), COALESCE(invalid_at, '')) >= ? "
            # A delete seen live for a consent the index never had (R1-07).
            "UNION SELECT uri FROM consent_tombstones WHERE did = ? AND deleted_at >= ?",
            (did, started, did, started),
        )
    }
    touched_videos = {
        row["uri"]
        for row in conn.execute(
            "SELECT uri FROM videos WHERE did = ? AND indexed_at >= ? "
            "UNION SELECT uri FROM video_tombstones WHERE did = ? AND deleted_at >= ?",
            (did, started, did, started),
        )
    }
    touched = {c: (touched_consents if c in CONSENT_NSIDS else touched_videos) for c in RECORD_NSIDS}
    for collection in RECORD_NSIDS:
        live = set()
        for r in listings[collection]:
            # listRecords comes from the contributor's PDS (untrusted): only
            # accept URIs in this repo and collection, and rebuild the URI
            # ourselves, so one repo can never overwrite another's rows.
            try:
                at = parse_at_uri(r.uri)
            except (ValueError, TypeError):
                invalid += 1
                click.echo(f"  skipped malformed URI {_safe(repr(r.uri))}")
                continue
            if at.did != did or at.collection != collection:
                invalid += 1
                _warn(f"skipped foreign URI {_safe(r.uri)} listed by {did}'s PDS")
                continue
            uri = f"at://{did}/{collection}/{at.rkey}"
            live.add(uri)
            if uri in touched[collection]:
                skipped += 1
                continue
            try:
                # On invalid records ingest itself makes sure older terms stop
                # being usable (an invalid row, a revoke, or a removed video).
                errors = av.ingest(conn, collection, uri, did, at.rkey, str(r.cid), to_dict(r.value))
            except Exception as e:  # noqa: BLE001 — poison record: skip it, but never keep old terms
                errors = [f"could not ingest: {type(e).__name__}"]
                av.invalidate(conn, collection, uri)
            if errors:
                invalid += 1
                click.echo(f"  skipped {_safe(uri)}: {_safe(errors[0])}")
            else:
                stored += 1
        # Anything indexed earlier that's gone now was deleted while we
        # weren't watching.
        table = "consents" if collection in CONSENT_NSIDS else "videos"
        prefix = f"at://{did}/{collection}/"
        gone = [
            row["uri"]
            for row in conn.execute(f"SELECT uri FROM {table} WHERE did = ?", (did,)).fetchall()
            if row["uri"].startswith(prefix) and row["uri"] not in live and row["uri"] not in touched[collection]
        ]
        for uri in gone:
            if collection in VIDEO_NSIDS:
                av.delete_video(conn, uri)
            else:
                av.delete_consent(conn, uri)
    av.set_account_signals(conn, did, account.bot, account.training_denied, started)
    # Hosting status from the PDS and the relay (getRepoStatus), never a
    # blind "active": a takedown seen live must not be undone (R1-10).
    acct = conn.execute("SELECT updated_at FROM accounts WHERE did = ?", (did,)).fetchone()
    if acct is None or acct["updated_at"] < started:
        if status is None:
            _warn(f"{did}: could not confirm the repo status with both the PDS and the relay; account status unchanged")
        else:
            av.set_account_status(conn, did, *status)
    if relay_lag:
        # The PDS is the authority and was just read, but the relay is not
        # (fully) forwarding this repo's events, so the live index can't keep
        # it current: keep it hidden and re-check it later (R3-01).
        lag_until = format_dt(datetime.now(UTC) + timedelta(seconds=av.RELAY_LAG_RECHECK_S))
        av.mark_relay_lag(conn, did, relay_lag, next_try=lag_until)
    else:
        # The listing started after any #sync seen so far: the repo is current.
        av.clear_desync(conn, did, started)
        av.clear_pending(conn, did, started)
    conn.commit()
    if skipped:
        click.echo(f"  {skipped} record(s) skipped (changed during backfill; the live index is newer)")
    if relay_lag:
        click.echo(f"{did}: relay reports {_safe(relay_lag)}; listed from the PDS but hidden until the relay catches up")
    return did, stored, invalid


def _relay_lag(answers) -> str | None:
    """The relay processing status among ``answers`` (throttled,
    host-throttled, desynchronized), if any."""
    from consentvid.appview import is_operational

    return next((a[1] for a in answers if is_operational(a)), None)


def _record_inactive_repo(conn, did: str, status: str | None, started: str) -> tuple[str, int, int]:
    """Record that ``did`` is not active, without listing it. Rows of a
    deactivated/taken-down/suspended account are kept (hidden by active=0;
    reactivation queues a backfill); a deleted account is purged."""
    from consentvid import appview as av

    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        acct = conn.execute("SELECT updated_at FROM accounts WHERE did = ?", (did,)).fetchone()
        if acct is None or acct["updated_at"] < started:  # a live #account event since then wins
            av.set_account_status(conn, did, False, status)
        av.clear_pending(conn, did, started)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    click.echo(f"{did}: account {status or 'inactive'}; not listed")
    return did, 0, 0


def _drain_repo(did: str) -> None:
    """Re-read one pending repo for the indexer's drain task (R3-02). Runs in
    a worker thread, so it opens its own database connection. _backfill_repo
    clears the pending row on success, or keeps it (due again in an hour)
    while the relay reports a processing status (R3-01)."""
    from consentvid import appview as av

    conn = av.connect()
    try:
        _backfill_repo(conn, did)
    finally:
        conn.close()


@appview.command("index")
@click.option("--stop-after", type=int, default=None, help="Exit after N relevant events.")
@click.option(
    "--no-drain",
    is_flag=True,
    help="Don't re-read pending repos (#sync, reactivations, relay lag) in the background; "
    "leave them to `appview backfill --pending`.",
)
def appview_index(stop_after, no_drain):
    """Follow the firehose (Jetstream) and keep the index current. Resumes from
    where it stopped. Repos queued for a re-read (a #sync, a reactivated
    account, a relay that reports throttled/desynchronized) are re-read in the
    background, one at a time, with backoff on failure."""
    from consentvid import appview as av

    conn = av.connect()
    s = av.stats(conn)
    click.echo(
        f"Indexing into {av.db_path()}"
        + (f", resuming from {s['cursor']}" if s["cursor"] else "")
        + " (Ctrl-C to stop)"
    )
    try:
        n = asyncio.run(
            av.index_loop(
                conn,
                on_event=lambda op, x: click.echo(f"  [{_safe(op)}] {_safe(x)}"),
                stop_after=stop_after,
                drain=None if no_drain else _drain_repo,
            )
        )
        click.echo(f"Processed {n} events.")
    except KeyboardInterrupt:
        click.echo("\nStopped.")


@appview.command("stats")
def appview_stats():
    """Show index counts and freshness."""
    from consentvid import appview as av

    conn = av.connect()
    s = av.stats(conn)
    for k, v in s.items():
        click.echo(f"{k + ':':<19}{v}")
    if s["gap_since"]:
        _warn(f"events after {s['gap_since']} may be missing; repair: consentvid appview backfill --all")
    elif not s["consents"] and not s["videos"]:
        click.echo("The index is empty; fill it with: consentvid appview backfill --all")
    if s["pending_backfill"]:
        click.echo(
            f"{s['pending_backfill']} repo(s) queued for a re-read (`appview index` re-reads them; "
            "`consentvid appview backfill --pending` forces it):"
        )
        for row in av.pending_rows(conn):
            due = f", next try {row['next_try']}" if row["next_try"] else ""
            click.echo(f"  {row['did']}: {_safe(row['reason'])} ({row['attempts']} failed attempt(s){due})")


@appview.command("revocations")
@click.option("--since", default=None, help="Only revocations after this ISO timestamp.")
@click.option("--json", "as_json", is_flag=True)
def appview_revocations(since, as_json):
    """Feed of withdrawn consents (poll with --since for incremental re-sync)."""
    from consentvid import appview as av

    rows = av.revocations(av.connect(), since)
    if as_json:
        click.echo(json.dumps([dict(r) for r in rows], indent=2))
        return
    for r in rows:
        click.echo(f"{_safe(r['revoked_at'])}  {_safe(r['uri'])}  ({_safe(r['revoke_reason'] or 'deleted')})")
    click.echo(f"{len(rows)} revocation(s)" + (f" since {_safe(since)}" if since else "") + ".")


@appview.command("query")
@_filter_options
@click.option(
    "--capture-provenance",
    "provenance",
    multiple=True,
    type=click.Choice(CAPTURE_PROVENANCE),
    callback=_refuse_attested,
    help="'attested' is refused: it is self-declared and nothing verifies it yet.",
)
@click.option("--repo", default=None, help="Only this contributor (handle or DID).")
@click.option("--include-revoked", is_flag=True, help="Also show withdrawn/inactive consents.")
@click.option("--include-expired", is_flag=True)
@_include_labeled_option
@click.option("--limit", default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def appview_query(
    purposes, model_classes, modalities, provenance, repo, include_revoked, include_expired, include_labeled,
    limit, as_json,
):
    """Find consented clips across contributors."""
    from consentvid import appview as av
    from consentvid.identity import resolve_did

    rows = av.query_consents(
        av.connect(),
        purposes=purposes,
        model_classes=model_classes,
        modalities=modalities,
        provenance=provenance,
        did=resolve_did(repo) if repo else None,
        include_revoked=include_revoked,
        include_expired=include_expired,
        include_labeled=include_labeled,
        limit=limit,
    )
    if as_json:
        click.echo(
            json.dumps(
                [{k: r[k] for k in r.keys() if k not in ("record", "video_record")} for r in rows], indent=2
            )
        )
        return
    def terms(col):
        return _safe(", ".join(map(str, json.loads(r[col]))))

    for r in rows:
        state = f"  REVOKED ({_safe(r['revoke_reason'])})" if r["revoked_at"] else ""
        click.secho(f"{_safe(repr(r['title']))}{state}", bold=True)
        click.echo(
            f"  consent:    {_safe(r['uri'])}\n  purposes:   {terms('purposes')}"
            f"\n  models:     {terms('model_classes')}"
            f"\n  modalities: {terms('modalities')}"
            f"\n  provenance: {_safe(r['capture_provenance'])}"
        )
    click.echo(f"{len(rows)} result(s).")


@appview.command("export")
@click.argument("output_path", type=click.Path(dir_okay=False, path_type=Path))
@_filter_options
@click.option(
    "--capture-provenance",
    "provenance",
    multiple=True,
    type=click.Choice(CAPTURE_PROVENANCE),
    callback=_refuse_attested,
    help="'attested' is refused: it is self-declared and nothing verifies it yet.",
)
@_include_labeled_option
@click.option("--name", default=None, help="Dataset name.")
@click.option("--description", default=None)
def appview_export(output_path, purposes, model_classes, modalities, provenance, include_labeled, name, description):
    """Build a multi-contributor Croissant-RAI manifest from the index.

    Run `consentvid resync` on the result before use: the index is fed by an
    unsigned firehose, resync verifies everything cryptographically.
    """
    from consentvid import appview as av
    from consentvid.croissant import Clip, build_manifest
    from consentvid.identity import display_name

    rows = av.query_consents(
        av.connect(),
        purposes=purposes,
        model_classes=model_classes,
        modalities=modalities,
        provenance=provenance,
        include_labeled=include_labeled,
        limit=None,
    )
    clips, missing = [], 0
    for r in rows:
        if not r["record"] or not r["video_record"]:
            missing += 1
            continue
        try:
            raw_consent, raw_video = json.loads(r["record"]), json.loads(r["video_record"])
            consent_nsid = parse_at_uri(r["uri"]).collection
            video_at = parse_at_uri(r["video_uri"], VIDEO_FOR_CONSENT[consent_nsid])
            # Re-validate: the index may predate today's rules.
            if validate_in_repo(consent_nsid, raw_consent, r["did"], rkey=r["rkey"]) or validate_in_repo(
                video_at.collection, raw_video, r["did"], rkey=video_at.rkey
            ):
                raise ValueError("no longer valid")
            consent = versions.consent_view(consent_nsid, raw_consent)
            video = versions.video_view(video_at.collection, raw_video, r["did"])
        except Exception as e:  # noqa: BLE001 — one bad row must not abort the export
            _warn(f"skipping indexed consent {_safe(r['uri'])}: {type(e).__name__}: {_safe(e)}")
            continue
        clips.append(
            Clip(
                r["uri"],
                r["cid"],
                consent,
                r["video_uri"],
                r["current_video_cid"],
                video,
                r["did"],
                display_name(r["did"]),
            )
        )
    if missing:
        _warn(
            f"{missing} indexed consent(s) predate full-record storage; "
            "re-run `appview backfill` for their contributors"
        )
    manifest, skipped = build_manifest(
        clips, name=name or f"consentvid-{date.today()}", description=description
    )
    if skipped:
        _warn(f"left out {len(skipped)} clip(s) without publicly reachable bytes")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2) + "\n")
    click.secho(
        f"Wrote {output_path}: {len(manifest['distribution'])} clip(s). "
        f"Next: consentvid resync {output_path}",
        fg="green",
    )


if __name__ == "__main__":
    main()
