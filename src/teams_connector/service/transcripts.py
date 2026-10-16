"""Searching meeting transcripts: the words in the transcripts of meeting recordings.

Two ways in, one result shape (docs/requirements.md §8, docs/roadmap.md P2.2f):

- **In conversations** (conversation_ids given, optionally with dates): the recording messages of those chats
  are read, and each recording's transcript is searched. The result is complete within the scope unless a
  transcript could not be read or the limit was reached.
- **Across meetings** (no conversations): Graph's search finds transcribed videos by their spoken words,
  which ranks them but does not prove they are meetings. Each video is kept only when a meeting chat holds a
  recording card naming that very drive item, which also gives its chat. The result is never complete: it is
  ranked, its total is approximate, videos whose chats were all read and hold no card are counted as not
  meetings, and videos whose chats could not all be read are counted as unverified, never as not meetings.

Terms are phrases matched inside one utterance (service/transcript_text.py). Pure matching and rendering
live in transcript_text; this module reads the chats, the transcripts and the search, and decides what a
result says.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from teams_connector.domain.errors import AuthenticationRequired, ConnectorError, InvalidRequest
from teams_connector.domain.models import (
    Chat,
    MeetingSummaryEntry,
    Message,
    RecordingRef,
    Transcript,
    TranscriptHit,
    TranscriptSearchCoverage,
    TranscriptSearchResult,
)
from teams_connector.remote.ports import TranscriptReader, VideoHit, VideoSearch
from teams_connector.service import transcript_text
from teams_connector.service.catalog import Catalog
from teams_connector.service.history import History, check_range
from teams_connector.service.summaries import MeetingSummaries

MAX_TERMS = 25
MAX_TERM_CHARS = 200
MAX_LIMIT = 100
MAX_CONVERSATIONS = 100
CANDIDATE_PAGE = 25  # videos per search request
MAX_CANDIDATES = 40  # videos checked per call across meetings: each costs a few chat reads
MAX_CHATS_PER_VIDEO = 25  # chats read to find one video's recording card
MAX_PAGES_PER_CHAT = 20  # history pages read per chat when looking for a card
CHAT_KINDS = ("meeting", "group", "one_on_one")  # where recording cards are posted


@dataclass
class _Tally:
    recordings: int = 0  # transcripts whose text was searched
    unavailable: int = 0  # recordings whose transcript could not be read
    untranscribed: int = 0  # recording cards Teams made no transcript for
    candidates: int = 0  # across meetings: videos checked
    not_meetings: int = 0  # across meetings: every chat that could hold the card was read, and none did
    unverified: int = 0  # across meetings: some chat that could hold the card could not be read
    channels_skipped: int = 0
    notes: list[str] = field(default_factory=list)
    # recordings with matches, by (conversation id, message id): a message id can repeat across chats
    matched: dict[tuple[str, str], RecordingRef] = field(default_factory=dict)


class TranscriptSearch:
    """Entry point for transcript search (MCP). Reads only; nothing is written to Teams."""

    def __init__(
        self,
        catalog: Catalog,
        history: History,
        transcripts: TranscriptReader,
        videos: VideoSearch,
        summaries: MeetingSummaries,
    ) -> None:
        self._catalog = catalog
        self._history = history
        self._transcripts = transcripts
        self._videos = videos
        self._summaries = summaries

    async def search(
        self,
        terms: list[str],
        *,
        conversation_ids: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 25,
        include_summaries: bool = True,
    ) -> TranscriptSearchResult:
        """Find the utterances that match any term. ``terms`` are checked here, once, at this entry point.

        With ``include_summaries``, the result also lists Teams' AI summary of each matched meeting once.
        """
        words = _checked_terms(terms)
        check_range(since, until)
        if not 1 <= limit <= MAX_LIMIT:
            raise InvalidRequest(f"limit must be between 1 and {MAX_LIMIT}.")
        if conversation_ids is not None:
            ids = list(dict.fromkeys(i.strip() for i in conversation_ids if i.strip()))
            if not ids or len(ids) > MAX_CONVERSATIONS:
                raise InvalidRequest(f"Pass 1 to {MAX_CONVERSATIONS} conversation ids, or omit them.")
            return await self._in_conversations(words, ids, since, until, limit, include_summaries)
        return await self._across_meetings(words, since, until, limit, include_summaries)

    # ------------------------------------------------------------------ in conversations

    async def _in_conversations(
        self,
        words: list[str],
        ids: list[str],
        since: datetime | None,
        until: datetime | None,
        limit: int,
        include_summaries: bool,
    ) -> TranscriptSearchResult:
        tally = _Tally()
        hits: list[TranscriptHit] = []
        seen: set[tuple[str, str]] = set()
        for conversation_id in ids:
            chat = await self._catalog.get(conversation_id)
            if chat.kind == "channel":
                tally.channels_skipped += 1
                continue
            async for page in self._history.selection(chat, since=since, until=until):
                for message in page:
                    recording = message.recording
                    if recording is None or (recording.drive_id, recording.item_id) in seen:
                        continue
                    seen.add((recording.drive_id, recording.item_id))
                    if not _transcribed(message):
                        tally.untranscribed += 1
                        continue
                    hits.extend(await self._hits_of(chat, message, recording, words, tally))
                    if len(hits) > limit:  # one match beyond the limit: there is more, so say so
                        return await self._result(
                            "conversations",
                            hits[:limit],
                            tally,
                            truncated=True,
                            exhaustive=True,
                            include_summaries=include_summaries,
                        )
        return await self._result(
            "conversations",
            hits,
            tally,
            truncated=False,
            exhaustive=True,
            include_summaries=include_summaries,
        )

    # ------------------------------------------------------------------ across meetings

    async def _across_meetings(
        self,
        words: list[str],
        since: datetime | None,
        until: datetime | None,
        limit: int,
        include_summaries: bool,
    ) -> TranscriptSearchResult:
        tally = _Tally()
        hits: list[TranscriptHit] = []
        offset = 0
        query = _video_query(words)
        while True:
            page, total = await self._videos.videos(query, offset=offset, size=CANDIDATE_PAGE)
            if not page:
                break
            for video in page:
                if not _in_dates(video, since, until):
                    continue
                if tally.candidates >= MAX_CANDIDATES:
                    return await self._across_result(
                        hits, tally, truncated=True, include_summaries=include_summaries
                    )
                tally.candidates += 1
                located = await self._recording_card(video, tally)
                if located is None:  # not a meeting, or not placed: already tallied
                    continue
                chat, message = located
                recording = _transcribed(message)
                if recording is None:
                    tally.untranscribed += 1
                    continue
                hits.extend(await self._hits_of(chat, message, recording, words, tally))
                if len(hits) > limit:
                    return await self._across_result(
                        hits[:limit], tally, truncated=True, include_summaries=include_summaries
                    )
            offset += len(page)
            if total is not None and offset >= total:
                break
        return await self._across_result(hits, tally, truncated=False, include_summaries=include_summaries)

    async def _across_result(
        self, hits: list[TranscriptHit], tally: _Tally, *, truncated: bool, include_summaries: bool
    ) -> TranscriptSearchResult:
        tally.notes.append(
            "Ranked search over the videos Graph returns: not a complete list of meetings, and the total is "
            "approximate."
        )
        return await self._result(
            "meetings",
            hits,
            tally,
            truncated=truncated,
            exhaustive=False,
            include_summaries=include_summaries,
        )

    async def _recording_card(self, video: VideoHit, tally: _Tally) -> tuple[Chat, Message] | None:
        """The meeting chat and recording message that name this video, or None. A video counts as not a
        meeting only when every chat that could hold its card was read to its end and none did; when some
        could not be read, it counts as unverified and is not listed."""
        if video.created_at is None:  # no time to bound the chats by
            tally.unverified += 1
            return None
        since = video.created_at - timedelta(hours=1)
        until = video.created_at + timedelta(days=1)
        chats = await self._catalog.active_since(since, CHAT_KINDS)
        exhaustive = len(chats) <= MAX_CHATS_PER_VIDEO
        for chat in chats[:MAX_CHATS_PER_VIDEO]:
            try:
                message, read_to_end = await self._card_in(chat, video, since, until)
            except AuthenticationRequired:
                raise
            except ConnectorError:  # one chat Teams refuses does not end the search
                exhaustive = False
                continue
            if message is not None:
                return chat, message
            exhaustive = exhaustive and read_to_end
        if exhaustive:
            tally.not_meetings += 1
        else:
            tally.unverified += 1
        return None

    async def _card_in(
        self, chat: Chat, video: VideoHit, since: datetime, until: datetime
    ) -> tuple[Message | None, bool]:
        """The recording message in one chat that names this video's drive item, and whether the chat was read
        to its end: False when the page bound stopped the reading first."""
        pages = 0
        async for page in self._history.selection(chat, since=since, until=until):
            pages += 1
            for message in page:
                recording = message.recording
                if (
                    recording is not None
                    and recording.item_id == video.item_id
                    and recording.drive_id == video.drive_id
                ):
                    return message, True
            if pages >= MAX_PAGES_PER_CHAT:
                return None, False
        return None, True

    # ------------------------------------------------------------------ shared

    async def _hits_of(
        self,
        chat: Chat,
        message: Message,
        recording: RecordingRef,
        words: list[str],
        tally: _Tally,
    ) -> list[TranscriptHit]:
        """Read one recording's transcript and return its matching utterances. A transcript that cannot be
        read is counted as unavailable, not as a search that found nothing."""
        tally.recordings += 1
        try:
            transcript = await self._transcripts.default(recording)
        except AuthenticationRequired:
            raise
        except ConnectorError:
            transcript = None
        if transcript is None:
            tally.unavailable += 1
            return []
        found = _matches(chat, message, recording, transcript, words)
        if found:
            tally.matched[(chat.id, message.id)] = recording
        return found

    async def _result(
        self,
        mode: Literal["conversations", "meetings"],
        hits: list[TranscriptHit],
        tally: _Tally,
        *,
        truncated: bool,
        exhaustive: bool,
        include_summaries: bool,
    ) -> TranscriptSearchResult:
        """The result. ``exhaustive`` says the search reads everything it claims to cover. A truncated result,
        one whose transcripts were not all read, or one that skipped a channel is never complete."""
        summaries: list[MeetingSummaryEntry] = []
        failed = 0
        if include_summaries:
            summaries, failed = await self._summaries_of(hits, tally)
        notes = list(tally.notes)
        if failed:
            notes.append(f"{failed} meeting summary(ies) could not be read; the matches are unaffected.")
        if tally.untranscribed:
            notes.append(f"{tally.untranscribed} recording(s) have no transcript in Teams.")
        if tally.unavailable:
            notes.append(
                f"{tally.unavailable} recording(s) had no readable transcript; they were not searched."
            )
        if tally.channels_skipped:
            notes.append("Channel recordings are not searched yet; channel conversations were skipped.")
        if tally.unverified:
            notes.append(
                f"{tally.unverified} video(s) could not be placed in a meeting chat, because some chat that "
                "might hold them could not be read; they are neither listed nor counted as non-meetings."
            )
        if truncated:
            notes.append(
                "More matches or videos may exist; narrow the terms, the dates or the conversations."
            )
        complete = exhaustive and not truncated and tally.unavailable == 0 and tally.channels_skipped == 0
        return TranscriptSearchResult(
            items=hits,
            coverage=TranscriptSearchCoverage(
                mode=mode,
                complete=complete,
                recordings=tally.recordings,
                transcripts_unavailable=tally.unavailable,
                candidates=tally.candidates,
                not_meetings=tally.not_meetings,
                unverified=tally.unverified,
                truncated=truncated,
                notes=notes,
            ),
            summaries=summaries,
        )

    async def _summaries_of(
        self, hits: list[TranscriptHit], tally: _Tally
    ) -> tuple[list[MeetingSummaryEntry], int]:
        """Teams' AI summary of each meeting the returned matches name, read once per meeting.

        Returns the summaries and the count of failed reads. The matches and completeness do not depend on it.
        Each returned match's recording is in ``tally.matched``, recorded by ``_hits_of``."""
        entries: list[MeetingSummaryEntry] = []
        failed = 0
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            key = (hit.conversation_id, hit.message_id)  # a message id can repeat across chats
            if key in seen:
                continue
            seen.add(key)
            read = await self._summaries.read(tally.matched[key])
            failed += 1 if read.failed else 0
            entries.append(
                MeetingSummaryEntry(
                    meeting=hit.meeting,
                    conversation_id=hit.conversation_id,
                    message_id=hit.message_id,
                    summary=read.text,
                    unavailable=read.failed,
                )
            )
        return entries, failed


def _transcribed(message: Message) -> RecordingRef | None:
    """The recording of a message whose card says Teams made a transcript, or None."""
    recording = message.recording
    if recording is None or not any(item.source == "transcript" for item in message.attachments):
        return None
    return recording


def _matches(
    chat: Chat,
    message: Message,
    recording: RecordingRef,
    transcript: Transcript,
    words: list[str],
) -> list[TranscriptHit]:
    hits: list[TranscriptHit] = []
    for entry in transcript.entries:
        term = next((term for term in words if transcript_text.matches(term, entry.text)), None)
        if term is None:
            continue
        hits.append(
            TranscriptHit(
                meeting=recording.title,
                conversation_id=chat.id,
                message_id=message.id,
                speaker=entry.speaker,
                offset=transcript_text.offset_label(entry.start),
                text=entry.text,
                term=term,
            )
        )
    return hits


def _checked_terms(terms: list[str]) -> list[str]:
    """One to 25 phrases, each non-empty, short, and without double quotes (the query is built from them)."""
    if not terms or len(terms) > MAX_TERMS:
        raise InvalidRequest(f"Pass 1 to {MAX_TERMS} terms.")
    checked: list[str] = []
    for term in terms:
        text = term.strip() if isinstance(term, str) else ""
        if not text or len(text) > MAX_TERM_CHARS or '"' in text:
            raise InvalidRequest(
                f"Each term must be 1 to {MAX_TERM_CHARS} characters, with no double quotes."
            )
        checked.append(text)
    return checked


def _video_query(words: list[str]) -> str:
    """Graph's search over video files for any of the phrases."""
    phrases = " OR ".join(f'"{word}"' for word in words)
    return f"({phrases}) filetype:mp4"


def _in_dates(video: VideoHit, since, until) -> bool:
    if video.created_at is None:
        return since is None and until is None
    if since is not None and video.created_at < since:
        return False
    return not (until is not None and video.created_at > until)
