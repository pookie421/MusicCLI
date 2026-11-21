"""Pure Black and White 1-Bit Pixel-Art Curses Interface for Pulse CLI."""

from __future__ import annotations

import curses
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from pulse.audio import AudioEngine, AudioState
from pulse.metadata import DitherAlgorithm, TrackMetadata, format_duration
from pulse.queue_manager import QueueManager, RepeatMode
from pulse.listening_history import ListeningHistory


class PulseApp:
    """1-Bit Monochrome Pixel-Art Terminal Audio Station."""

    # 1-Bit dithering & block characters
    DITHER = [" ", "░", "▒", "▓", "█"]
    BLOCKS = [" ", " ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]

    def __init__(self, initial_path: Optional[Path | str] = None, auto_play_file: Optional[Path | str] = None):
        self.initial_path = Path(initial_path).resolve() if initial_path else None
        self.auto_play_file = Path(auto_play_file).resolve() if auto_play_file else None

        if self.initial_path and self.initial_path.is_dir():
            self.current_dir = self.initial_path
        elif self.auto_play_file:
            self.current_dir = self.auto_play_file.parent
        else:
            music_dir = Path.home() / "Music"
            self.current_dir = music_dir if music_dir.exists() else Path.cwd()

        self.audio = AudioEngine()
        self.queue = QueueManager()

        # Browser state
        self.browser_items: List[Tuple[str, Path, bool]] = []
        self.browser_cursor: int = 0
        self.browser_scroll: int = 0
        self.filter_query: str = ""
        self.is_filtering: bool = False

        # Queue state
        self.queue_cursor: int = 0
        self.queue_scroll: int = 0

        # Pane focus: 0 = Browser, 1 = Queue, 2 = Listening History
        self.active_pane: int = 0
        self.history = ListeningHistory()
        self.history_cursor: int = 0
        self.history_scroll: int = 0
        self.loop_current_track: bool = False
        self.show_help: bool = False
        self.show_cover_modal: bool = False
        self.cover_modal_track: Optional[TrackMetadata] = None
        self._meta_cache: dict[Path, TrackMetadata] = {}
        self.status_message: str = ""
        self.status_time: float = 0.0
        self.volume_overlay_time: float = 0.0

        # Dither display configuration
        self.dither_algo: DitherAlgorithm = DitherAlgorithm.ATKINSON
        self._last_terminal_title: str = ""

        self._load_directory(self.current_dir)

        if self.auto_play_file and self.auto_play_file.exists():
            meta = TrackMetadata.from_path(self.auto_play_file)
            self.queue.add_track(meta)
            self.queue.current_index = 0
            self._load_track(meta)

    def _load_track(self, metadata: TrackMetadata) -> bool:
        """Load a track and record a local listening-history entry on success."""
        loaded = self.audio.load_track(metadata)
        if loaded:
            if not self.history.add(metadata):
                self.set_status("HISTORY: COULD NOT SAVE ENTRY")
        return loaded

    def set_status(self, msg: str) -> None:
        self.status_message = msg
        self.status_time = time.time()

    def _load_directory(self, path: Path) -> None:
        """Scan directory for subdirectories, MP3, and FLAC files."""
        self.current_dir = path.resolve()
        items: List[Tuple[str, Path, bool]] = []

        if self.current_dir.parent != self.current_dir:
            items.append(("..", self.current_dir.parent, True))

        dirs: List[Tuple[str, Path, bool]] = []
        files: List[Tuple[str, Path, bool]] = []

        try:
            for entry in self.current_dir.iterdir():
                name = entry.name
                if name.startswith(".") or name in ("node_modules", "__pycache__", "venv", ".venv"):
                    continue

                if self.filter_query and self.filter_query.lower() not in name.lower():
                    continue

                if entry.is_dir():
                    dirs.append((f"{name}/", entry, True))
                elif entry.is_file() and entry.suffix.lower() in (".mp3", ".flac"):
                    files.append((name, entry, False))
        except (PermissionError, OSError):
            self.set_status(f"ERR: ACCESS DENIED: {self.current_dir}")

        dirs.sort(key=lambda x: x[0].lower())
        files.sort(key=lambda x: x[0].lower())

        self.browser_items = items + dirs + files
        self.browser_cursor = min(self.browser_cursor, max(0, len(self.browser_items) - 1))
        self.browser_scroll = 0

    def run(self) -> None:
        try:
            curses.wrapper(self._main_loop)
        finally:
            self.audio.close()

    def _init_colors(self) -> None:
        """Enforce strict high-contrast monochrome (Black & White)."""
        curses.start_color()
        # Pair 1: Normal White on Black
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)
        # Pair 2: Reverse Inverted Black on White (1-Bit Solid Highlight)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE)

    def _update_terminal_title(self) -> None:
        """Dynamically update terminal emulator window/tab title using VT100 OSC sequence."""
        track = self.queue.current_track
        if track:
            if self.audio.state == AudioState.PLAYING:
                icon = "▶"
            elif self.audio.state == AudioState.PAUSED:
                icon = "⏸"
            else:
                icon = "■"
            title_str = f"{icon} {track.artist} - {track.title} // PULSE"
        else:
            title_str = "■ PULSE CLI // [IDLE]"

        if title_str != self._last_terminal_title:
            self._last_terminal_title = title_str
            try:
                sys.stdout.write(f"\033]0;{title_str}\007")
                sys.stdout.flush()
            except Exception:
                pass

    def _main_loop(self, stdscr) -> None:
        self.stdscr = stdscr
        curses.curs_set(0)
        stdscr.timeout(40)  # 25 FPS update rate
        self._init_colors()

        while True:
            if self.audio.check_and_clear_finished():
                if self.loop_current_track and self.queue.current_track:
                    # Restarting a loop is part of the same listening session.
                    self.audio.load_track(self.queue.current_track)
                else:
                    next_t = self.queue.next_track(auto_advance=True)
                    if next_t:
                        self._load_track(next_t)
                    else:
                        self.audio.stop()

            self._update_terminal_title()
            self._render()

            try:
                ch = stdscr.getch()
            except Exception:
                ch = -1

            if ch != -1:
                if not self._handle_input(ch):
                    break

    def _handle_input(self, ch: int) -> bool:
        if self.show_cover_modal:
            self.show_cover_modal = False
            return True

        if self.show_help:
            self.show_help = False
            return True

        if self.is_filtering:
            return self._handle_filter_input(ch)

        if ch in (ord('q'), ord('Q')):
            return False

        elif ch == ord('?'):
            self.show_help = True

        elif ch in (ord('v'), ord('V')):
            target_track: Optional[TrackMetadata] = None
            if self.active_pane == 0 and self.browser_items:
                name, path, is_dir = self.browser_items[self.browser_cursor]
                if not is_dir and path.suffix.lower() in (".mp3", ".flac"):
                    if path not in self._meta_cache:
                        self._meta_cache[path] = TrackMetadata.from_path(path)
                    target_track = self._meta_cache[path]
            elif self.active_pane == 1 and self.queue.tracks:
                if 0 <= self.queue_cursor < len(self.queue.tracks):
                    target_track = self.queue.tracks[self.queue_cursor]

            if not target_track:
                target_track = self.queue.current_track

            if target_track:
                self.cover_modal_track = target_track
                self.show_cover_modal = True
                src_tag = f" ({target_track.cover_source.upper()})" if target_track.cover_source else ""
                self.set_status(f"ALBUM ART EXPANDED{src_tag}")
            else:
                self.set_status("NO TRACK SELECTED")

        elif ch == ord(' '):
            if self.audio.state == AudioState.STOPPED:
                track = self.queue.current_track
                if track:
                    self._load_track(track)
                elif self.queue.count > 0:
                    self.queue.current_index = 0
                    self._load_track(self.queue.tracks[0])
            else:
                self.audio.toggle_play_pause()

        elif ch == ord('\t'):
            self.active_pane = (self.active_pane + 1) % 3

        elif ch == ord('1'):
            self.active_pane = 0
            self.set_status("PANE: [1] FILES")

        elif ch == ord('2'):
            self.active_pane = 1
            self.set_status("PANE: [2] QUEUE")

        elif ch == ord('3'):
            self.active_pane = 2
            self.set_status("PANE: [3] LISTENING HISTORY")

        elif ch == ord('/'):
            self.active_pane = 0
            self.is_filtering = True
            self.filter_query = ""
            self._load_directory(self.current_dir)

        elif ch in (ord('+'), ord('='), ord(']')):
            self.audio.change_volume(0.05)
            self.volume_overlay_time = time.time()

        elif ch in (ord('-'), ord('_'), ord('[')):
            self.audio.change_volume(-0.05)
            self.volume_overlay_time = time.time()

        elif ch in (ord('m'), ord('M')):
            muted = self.audio.toggle_mute()
            self.set_status("AUDIO: MUTED" if muted else f"AUDIO: {int(self.audio.volume * 100)}%")

        elif ch in (curses.KEY_LEFT, ord(',')):
            self.audio.seek(-5.0)

        elif ch in (curses.KEY_RIGHT, ord('.')):
            self.audio.seek(5.0)

        elif ch in (ord('n'), ord('N')):
            next_t = self.queue.next_track()
            if next_t:
                self._load_track(next_t)
            else:
                self.set_status("QUEUE: END REACHED")

        elif ch in (ord('p'), ord('P')):
            prev_t = self.queue.prev_track()
            if prev_t:
                self._load_track(prev_t)

        elif ch in (ord('r'), ord('R')):
            mode = self.queue.toggle_repeat()
            self.set_status(f"LOOP: {mode.name}")

        elif ch in (ord('s'), ord('S')):
            shuf = self.queue.toggle_shuffle()
            self.set_status(f"SHUFFLE: {'ON' if shuf else 'OFF'}")

        elif ch in (ord('t'), ord('T')):
            self.dither_algo = self.dither_algo.next_algorithm()
            self.set_status(f"DITHER: {self.dither_algo.value}")

        elif ch in (ord('g'), ord('G')):
            rg_state = self.audio.toggle_replay_gain()
            self.set_status(f"REPLAYGAIN: {'ON' if rg_state else 'OFF'}")

        elif ch in (ord('l'), ord('L')):
            self.loop_current_track = not self.loop_current_track
            self.set_status(f"TRACK LOOP: {'ON' if self.loop_current_track else 'OFF'}")

        if self.active_pane == 0:
            self._handle_browser_input(ch)
        elif self.active_pane == 1:
            self._handle_queue_input(ch)
        else:
            self._handle_history_input(ch)

        return True

    def _handle_history_input(self, ch: int) -> None:
        if ch in (curses.KEY_UP, ord('k')):
            self.history_cursor = max(0, self.history_cursor - 1)
        elif ch in (curses.KEY_DOWN, ord('j')):
            self.history_cursor = min(max(0, len(self.history.entries) - 1), self.history_cursor + 1)
        elif ch in (10, 13, curses.KEY_ENTER) and self.history.entries:
            entry = self.history.entries[self.history_cursor]
            path = Path(entry.get("path", ""))
            if path.is_file():
                metadata = TrackMetadata.from_path(path)
                idx = self.queue.add_track(metadata)
                self.queue.current_index = idx
                self.queue_cursor = idx
                self._load_track(metadata)
            else:
                self.set_status("HISTORY: FILE NO LONGER EXISTS")
        elif ch in (ord('c'), ord('C')):
            if self.history.clear():
                self.history_cursor = 0
                self.set_status("HISTORY: CLEARED")
            else:
                self.set_status("HISTORY: COULD NOT CLEAR")

    def _handle_filter_input(self, ch: int) -> bool:
        if ch in (10, 13, curses.KEY_ENTER):
            self.is_filtering = False
            self._load_directory(self.current_dir)
        elif ch == 27:  # Escape
            self.is_filtering = False
            self.filter_query = ""
            self._load_directory(self.current_dir)
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.filter_query = self.filter_query[:-1]
            self._load_directory(self.current_dir)
        elif 32 <= ch <= 126:
            self.filter_query += chr(ch)
            self._load_directory(self.current_dir)
        return True

    def _handle_browser_input(self, ch: int) -> None:
        count = len(self.browser_items)
        if count == 0:
            if ch in (curses.KEY_BACKSPACE, 127, 8, ord('h')):
                if self.current_dir.parent != self.current_dir:
                    self._load_directory(self.current_dir.parent)
            return

        if ch in (curses.KEY_UP, ord('k')):
            self.browser_cursor = max(0, self.browser_cursor - 1)
        elif ch in (curses.KEY_DOWN, ord('j')):
            self.browser_cursor = min(count - 1, self.browser_cursor + 1)
        elif ch == curses.KEY_PPAGE:
            self.browser_cursor = max(0, self.browser_cursor - 10)
        elif ch == curses.KEY_NPAGE:
            self.browser_cursor = min(count - 1, self.browser_cursor + 10)
        elif ch in (curses.KEY_BACKSPACE, 127, 8, ord('h')):
            if self.current_dir.parent != self.current_dir:
                self._load_directory(self.current_dir.parent)
        elif ch in (10, 13, curses.KEY_ENTER):
            name, path, is_dir = self.browser_items[self.browser_cursor]
            if is_dir:
                self._load_directory(path)
            else:
                meta = TrackMetadata.from_path(path)
                idx = self.queue.add_track(meta)
                self.queue.current_index = idx
                self._load_track(meta)
        elif ch in (ord('a'), ord('A')):
            name, path, is_dir = self.browser_items[self.browser_cursor]
            if is_dir:
                added = 0
                found = []
                for pat in ("*.mp3", "*.MP3", "*.flac", "*.FLAC"):
                    found.extend(path.glob(pat))
                found.sort(key=lambda p: p.name.lower())
                for f in found:
                    self.queue.add_track(TrackMetadata.from_path(f))
                    added += 1
                self.set_status(f"QUEUED: {added} TRACKS ({path.name}/)")
            else:
                meta = TrackMetadata.from_path(path)
                self.queue.add_track(meta)
                self.set_status(f"QUEUED: {meta.title}")

    def _handle_queue_input(self, ch: int) -> None:
        count = self.queue.count
        if count == 0:
            return

        if ch in (curses.KEY_UP, ord('k')):
            self.queue_cursor = max(0, self.queue_cursor - 1)
        elif ch in (curses.KEY_DOWN, ord('j')):
            self.queue_cursor = min(count - 1, self.queue_cursor + 1)
        elif ch in (10, 13, curses.KEY_ENTER):
            track = self.queue.jump_to(self.queue_cursor)
            if track:
                self._load_track(track)
        elif ch in (ord('d'), ord('x'), curses.KEY_DC):
            removing_current = self.queue_cursor == self.queue.current_index
            previous_audio_state = self.audio.state
            if removing_current:
                self.queue.next_track()

            removed = self.queue.remove_at(self.queue_cursor)
            if removed:
                self.set_status(f"REMOVED: {removed.title}")
                if self.queue.count == 0:
                    self.audio.unload()
                elif removing_current:
                    replacement = self.queue.current_track
                    if replacement:
                        self._load_track(replacement)
                        if previous_audio_state == AudioState.PAUSED:
                            self.audio.pause()
                        elif previous_audio_state == AudioState.STOPPED:
                            self.audio.stop()
            self.queue_cursor = min(self.queue_cursor, max(0, self.queue.count - 1))
        elif ch in (ord('c'), ord('C')):
            self.queue.clear()
            self.audio.unload()
            self.queue_cursor = 0
            self.set_status("QUEUE: CLEARED")

    def _render(self) -> None:
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()

        if h < 16 or w < 60:
            self._safe_addstr(0, 0, "TERMINAL SIZE TOO SMALL. MIN: 80x24", curses.A_BOLD)
            self.stdscr.refresh()
            return

        if self.show_cover_modal and self.cover_modal_track:
            self._render_cover_modal(h, w)
            self.stdscr.refresh()
            return

        if self.show_help:
            self._render_help_screen(h, w)
            self.stdscr.refresh()
            return

        # 1. Top Header Box
        self._render_header(w)

        # 2. Main Split View: Files | Queue | Listening History
        if h >= 28:
            vis_h = 6
            tele_h = 5
        elif h >= 24:
            vis_h = 5
            tele_h = 5
        else:
            vis_h = 4
            tele_h = 4

        main_h = max(5, h - vis_h - tele_h - 3)

        if w >= 96:
            col1_w = int(w * 0.36)
            col2_w = int(w * 0.34)
            col3_w = w - col1_w - col2_w
            self._render_browser_pane(2, 0, main_h, col1_w)
            self._render_queue_pane(2, col1_w, main_h, col2_w)
            self._render_history_pane(2, col1_w + col2_w, main_h, col3_w)
        elif w >= 70:
            half_w = w // 2
            rem_w = w - half_w
            self._render_browser_pane(2, 0, main_h, half_w)
            if self.active_pane == 2:
                self._render_history_pane(2, half_w, main_h, rem_w)
            else:
                self._render_queue_pane(2, half_w, main_h, rem_w)
        else:
            if self.active_pane == 0:
                self._render_browser_pane(2, 0, main_h, w)
            elif self.active_pane == 1:
                self._render_queue_pane(2, 0, main_h, w)
            else:
                self._render_history_pane(2, 0, main_h, w)

        # 3. Now playing title and playback progress
        vis_y = 2 + main_h
        self._render_playback_box(vis_y, 0, vis_h, w)

        # 4. Playback Scrubber & Telemetry Box
        tele_y = vis_y + vis_h
        self._render_telemetry_box(tele_y, 0, tele_h, w)

        # 5. Bottom Solid Command Strip
        self._render_footer(h - 1, w)

        self.stdscr.refresh()

    def _safe_addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        max_len = w - x
        if max_len <= 0:
            return
        try:
            self.stdscr.addstr(y, x, text[:max_len], attr)
        except curses.error:
            pass

    def _render_header(self, w: int) -> None:
        """Sharp pixel-art title bar."""
        bar = " " * w
        self._safe_addstr(0, 0, bar, curses.color_pair(2))
        title = " ■ PULSE // AUDIO ENGINE 1.0 "
        self._safe_addstr(0, 0, title, curses.color_pair(2) | curses.A_BOLD)

        path_display = f" {self.current_dir} "
        if w > len(title) + len(path_display) + 12:
            self._safe_addstr(0, len(title) + 2, path_display, curses.color_pair(2))

        right_hint = "[?] HELP  [1/2/3] PANES  [V] COVER  [Q] QUIT "
        if w > len(title) + len(right_hint):
            self._safe_addstr(0, w - len(right_hint), right_hint, curses.color_pair(2) | curses.A_BOLD)

        # Brief volume slider replaces the divider after volume changes.
        if time.time() - self.volume_overlay_time < 1.5:
            level = max(0, min(10, int(round(self.audio.volume * 10))))
            slider = f" VOL [{('█' * level) + ('░' * (10 - level))}] {int(self.audio.volume * 100)}% "
            self._safe_addstr(1, 0, "─" * w, curses.A_DIM)
            self._safe_addstr(1, max(0, (w - len(slider)) // 2), slider, curses.color_pair(2) | curses.A_BOLD)
        else:
            self._safe_addstr(1, 0, "─" * w, curses.A_DIM)

    def _render_browser_pane(self, top: int, left: int, height: int, width: int) -> None:
        """Render 1-bit pixel box for file browser."""
        is_active = (self.active_pane == 0)
        box_attr = curses.A_BOLD if is_active else curses.A_DIM

        # Frame Header and always-visible song search field
        title = "┌─[ 1: FILES ]"
        rem_w = max(0, width - len(title) - 1)
        self._safe_addstr(top, left, f"{title}{'─' * rem_w}┐", box_attr)

        search_label = f"/ Search songs: {self.filter_query}" if self.filter_query else "/ Search songs..."
        search_text = f"│ {search_label:<{width - 4}} │"
        search_attr = curses.color_pair(2) | curses.A_BOLD if self.is_filtering else curses.A_BOLD
        self._safe_addstr(top + 1, left, search_text, search_attr)

        content_h = height - 3
        count = len(self.browser_items)

        if self.browser_cursor < self.browser_scroll:
            self.browser_scroll = self.browser_cursor
        elif self.browser_cursor >= self.browser_scroll + content_h:
            self.browser_scroll = self.browser_cursor - content_h + 1

        for idx in range(content_h):
            row_y = top + 2 + idx
            item_idx = self.browser_scroll + idx

            if item_idx < count:
                name, path, is_dir = self.browser_items[item_idx]
                is_selected = (item_idx == self.browser_cursor)

                if is_selected and is_active:
                    attr = curses.color_pair(2) | curses.A_BOLD  # Solid white highlight
                    marker = "▶ "
                elif is_selected:
                    attr = curses.A_BOLD
                    marker = "▷ "
                else:
                    attr = curses.A_NORMAL
                    marker = "  "

                if is_dir:
                    tag = "[DIR] "
                elif path.suffix.lower() == ".flac":
                    tag = "[FLAC]"
                else:
                    tag = "[MP3] "
                entry_str = f"{marker}{tag}{name}"
                fill_len = width - 4
                self._safe_addstr(row_y, left, "│ ", box_attr)
                self._safe_addstr(row_y, left + 2, f"{entry_str:<{fill_len}}", attr)
                self._safe_addstr(row_y, left + width - 1, "│", box_attr)
            else:
                self._safe_addstr(row_y, left, "│" + (" " * (width - 2)) + "│", box_attr)

        self._safe_addstr(top + height - 1, left, "└" + ("─" * (width - 2)) + "┘", box_attr)

    def _render_queue_pane(self, top: int, left: int, height: int, width: int) -> None:
        """Render 1-bit pixel box for queue."""
        is_active = (self.active_pane == 1)
        box_attr = curses.A_BOLD if is_active else curses.A_DIM

        title = f"┌─[ 2: QUEUE ({self.queue.count}) ]"
        rem_w = max(0, width - len(title) - 1)
        self._safe_addstr(top, left, f"{title}{'─' * rem_w}┐", box_attr)

        content_h = height - 2
        count = self.queue.count

        if self.queue_cursor < self.queue_scroll:
            self.queue_scroll = self.queue_cursor
        elif self.queue_cursor >= self.queue_scroll + content_h:
            self.queue_scroll = self.queue_cursor - content_h + 1

        for idx in range(content_h):
            row_y = top + 1 + idx
            item_idx = self.queue_scroll + idx

            if item_idx < count:
                track = self.queue.tracks[item_idx]
                is_selected = (item_idx == self.queue_cursor)
                is_playing = (item_idx == self.queue.current_index)

                if is_selected and is_active:
                    attr = curses.color_pair(2) | curses.A_BOLD
                    marker = "▶ "
                elif is_selected:
                    attr = curses.A_BOLD
                    marker = "▷ "
                elif is_playing:
                    attr = curses.A_BOLD
                    marker = "■ "
                else:
                    attr = curses.A_NORMAL
                    marker = "  "

                dur = track.formatted_duration
                num_str = f"{item_idx + 1:02d}. "
                avail_title = max(8, width - 18)
                title_str = track.title[:avail_title]
                entry_str = f"{marker}{num_str}{title_str:<{avail_title}} {dur:>6s}"

                self._safe_addstr(row_y, left, "│ ", box_attr)
                self._safe_addstr(row_y, left + 2, f"{entry_str:<{width - 4}}", attr)
                self._safe_addstr(row_y, left + width - 1, "│", box_attr)
            else:
                self._safe_addstr(row_y, left, "│" + (" " * (width - 2)) + "│", box_attr)

        self._safe_addstr(top + height - 1, left, "└" + ("─" * (width - 2)) + "┘", box_attr)

    def _render_history_pane(self, top: int, left: int, height: int, width: int) -> None:
        """Render recent local playback sessions in pane 3."""
        is_active = self.active_pane == 2
        box_attr = curses.A_BOLD if is_active else curses.A_DIM
        title = f"┌─[ 3: LISTENING HISTORY ({len(self.history.entries)}) ]"
        self._safe_addstr(top, left, f"{title}{'─' * max(0, width - len(title) - 1)}┐", box_attr)
        content_h = max(0, height - 2)
        entries = self.history.entries
        if entries:
            self.history_cursor = min(self.history_cursor, len(entries) - 1)
        else:
            self.history_cursor = 0
        if self.history_cursor < self.history_scroll:
            self.history_scroll = self.history_cursor
        elif self.history_cursor >= self.history_scroll + max(1, content_h):
            self.history_scroll = self.history_cursor - max(1, content_h) + 1

        for row in range(content_h):
            entry_idx = self.history_scroll + row
            y = top + 1 + row
            if entry_idx < len(entries):
                entry = entries[entry_idx]
                selected = entry_idx == self.history_cursor
                marker = "▶ " if selected and is_active else ("▷ " if selected else "  ")
                title_text = entry.get("title", "Unknown Title")
                artist = entry.get("artist", "Unknown Artist")
                album = entry.get("album", "")
                played_at = entry.get("played_at", "")
                time_text = played_at[11:16] if len(played_at) >= 16 else "--:--"
                label = f"{marker}{time_text}  {title_text} — {artist}"
                if album and width >= 42:
                    label += f"  [{album}]"
                attr = curses.color_pair(2) | curses.A_BOLD if selected and is_active else (curses.A_BOLD if selected else curses.A_NORMAL)
                self._safe_addstr(y, left, "│ ", box_attr)
                self._safe_addstr(y, left + 2, f"{label:<{max(0, width - 4)}}", attr)
                self._safe_addstr(y, left + width - 1, "│", box_attr)
            else:
                self._safe_addstr(y, left, "│" + (" " * max(0, width - 2)) + "│", box_attr)
        if not entries and content_h:
            self._safe_addstr(top + 1 + content_h // 2, left + 2, "No listening history yet"[:max(0, width - 4)], curses.A_DIM)
        self._safe_addstr(top + height - 1, left, "└" + ("─" * (width - 2)) + "┘", box_attr)

    def _render_playback_box(self, top: int, left: int, height: int, width: int) -> None:
        """Render the current song title, timestamp, and a monochrome progress bar."""
        track = self.queue.current_track
        curr_sec, total_sec, pct = self.audio.get_progress()
        state = self.audio.state
        state_tag = "PLAYING" if state == AudioState.PLAYING else "PAUSED" if state == AudioState.PAUSED else "STOPPED"
        if self.loop_current_track:
            state_tag += " ↻"
        title = "┌─[ NOW PLAYING ]"
        self._safe_addstr(top, left, f"{title}{'─' * max(0, width - len(title) - 1)}┐", curses.A_BOLD)

        inner_w = max(1, width - 4)
        song = f"{track.artist} — {track.title}" if track and track.artist != "Unknown Artist" else (track.title if track else "No track loaded")
        stamp = f"{format_duration(curr_sec)} / {format_duration(total_sec)}"
        bar_w = max(1, inner_w - len(stamp) - len(state_tag) - 4)
        filled = min(bar_w, int(bar_w * pct / 100))
        bar = "█" * filled + "░" * (bar_w - filled)
        progress_line = f"{state_tag} [{bar}] {stamp}"

        if height >= 5:
            self._safe_addstr(top + 1, left, f"│ {song[:inner_w]:<{inner_w}} │", curses.A_BOLD)
            self._safe_addstr(top + 2, left, f"│ {progress_line:<{inner_w}} │", curses.A_NORMAL)
            for row in range(top + 3, top + height - 1):
                self._safe_addstr(row, left, "│" + (" " * (width - 2)) + "│", curses.A_NORMAL)
        elif height >= 4:
            self._safe_addstr(top + 1, left, f"│ {song[:inner_w]:<{inner_w}} │", curses.A_BOLD)
            self._safe_addstr(top + 2, left, f"│ {progress_line:<{inner_w}} │", curses.A_NORMAL)
        self._safe_addstr(top + height - 1, left, "└" + ("─" * (width - 2)) + "┘", curses.A_NORMAL)

    def _render_telemetry_box(self, top: int, left: int, height: int, width: int) -> None:
        """Render 1-bit pixel playback telemetry, dithered progress, and mode meters."""
        self._safe_addstr(top, left, "┌─[ NOW PLAYING & COVER ]" + ("─" * max(0, width - 26)) + "┐", curses.A_NORMAL)

        track = self.queue.current_track
        curr_sec, tot_sec, pct = self.audio.get_progress()

        inner_h = max(2, height - 2)

        # Show side-by-side cover thumbnail when width >= 50 and height >= 4
        if width >= 50 and height >= 4:
            art_w = 12 if width < 80 else 14
            art_lines = track.get_pixel_art(art_w, inner_h, algorithm=self.dither_algo) if track else TrackMetadata.empty_pixel_art(art_w, inner_h)
            info_w = max(10, width - 4 - art_w - 3)

            # Row 0: Status & Track Title
            if track:
                state_tag = "[PLAY]" if self.audio.state == AudioState.PLAYING else "[PAUSE]"
                if self.audio.state == AudioState.STOPPED:
                    state_tag = "[STOP]"
                if self.loop_current_track:
                    state_tag += "↻"
                artist_title = f"{track.artist} - {track.title}" if track.artist != "Unknown Artist" else track.title
                specs = f"[{track.file_format} {track.bitrate}k / {track.channels.upper()}]"
                avail_title = max(8, info_w - len(state_tag) - len(specs) - 2)
                t_str = artist_title[:avail_title]
                row0 = f"{state_tag} {t_str:<{avail_title}} {specs}"
            else:
                row0 = "[IDLE] NO TRACK LOADED"

            # Row 1: Album & Genre
            if track:
                alb_str = f"ALBUM: {track.album}" + (f" ({track.year})" if track.year else "")
                if track.genre:
                    alb_str += f"  GENRE: {track.genre}"
                row1 = alb_str[:info_w]
            else:
                row1 = "BROWSE AND SELECT AUDIO WITH [ENTER]"

            # Row 2: Dithered Scrubber Bar
            curr_str = format_duration(curr_sec)
            tot_str = format_duration(tot_sec)
            rem_sec = max(0.0, tot_sec - curr_sec)
            rem_str = f"-{format_duration(rem_sec)}"
            t_pre = f"{curr_str} ["
            t_post = f"] {tot_str} ({rem_str}) {pct:5.1f}%"
            fixed_len = len(t_pre) + len(t_post)
            bar_w = max(6, info_w - fixed_len)
            if tot_sec > 0:
                filled_len = int((curr_sec / tot_sec) * bar_w)
                dither_len = min(2, filled_len)
                solid_len = max(0, filled_len - dither_len)
                empty_len = max(0, bar_w - filled_len)
                bar_chars = ("█" * solid_len) + ("▓" * dither_len) + ("░" * empty_len)
            else:
                bar_chars = "░" * bar_w
            row2 = f"{t_pre}{bar_chars}{t_post}"

            # Row 3: Mode / Volume / Cover key
            now = time.time()
            if self.is_filtering:
                row3 = f"QUERY: {self.filter_query}_ [ENTER: CONFIRM  ESC: CANCEL]"
            elif self.status_message and (now - self.status_time < 3.0):
                row3 = f"STATUS >> {self.status_message}"
            else:
                vol_lvl = int(self.audio.volume * 10)
                vol_bar = ("█" * vol_lvl) + ("░" * (10 - vol_lvl))
                vol_str = f"VOL:[{vol_bar}]"
                rep_str = f"LOOP:{'ON' if self.loop_current_track else 'OFF'}"
                queue_rep = f"QUEUE:{self.queue.repeat_mode.name}"
                shuf_str = f"SHUF:{'ON' if self.queue.is_shuffled else 'OFF'}"
                trk_str = f"TRK:{self.queue.current_index + 1:02d}/{self.queue.count:02d}" if self.queue.count else "TRK:--/--"
                dith_str = f"DITH:{self.dither_algo.value[:3]}"
                cover_hint = "[V] ART"
                row3 = f"{vol_str} │ {rep_str} │ {queue_rep} │ {shuf_str} │ {dith_str} │ {trk_str} │ {cover_hint}"

            rows = [row0, row1, row2, row3]
            while len(rows) < inner_h:
                rows.append("")

            for idx in range(inner_h):
                row_y = top + 1 + idx
                art = art_lines[idx] if idx < len(art_lines) else (" " * art_w)
                text = rows[idx]
                self._safe_addstr(row_y, left, "│ ", curses.A_NORMAL)
                self._safe_addstr(row_y, left + 2, art, curses.A_NORMAL)
                self._safe_addstr(row_y, left + 2 + art_w, " │ ", curses.A_DIM)
                self._safe_addstr(row_y, left + 2 + art_w + 3, f"{text:<{info_w}}"[:info_w], curses.A_BOLD if idx == 0 else curses.A_NORMAL)
                self._safe_addstr(row_y, left + width - 1, "│", curses.A_NORMAL)

            self._safe_addstr(top + height - 1, left, "└" + ("─" * (width - 2)) + "┘", curses.A_NORMAL)
        else:
            # Compact fallback for small windows
            if track:
                state_tag = "[PLAY]" if self.audio.state == AudioState.PLAYING else "[PAUSE]"
                if self.audio.state == AudioState.STOPPED:
                    state_tag = "[STOP]"
                if self.loop_current_track:
                    state_tag += "↻"
                artist_title = f"{track.artist} - {track.title}" if track.artist != "Unknown Artist" else track.title
                specs = f"[{track.file_format} {track.bitrate}k / {track.channels.upper()}]"
                avail_w = max(6, width - len(state_tag) - len(specs) - 6)
                t_str = artist_title[:avail_w]
                line1 = f"│ {state_tag} {t_str:<{avail_w}} {specs} │"
            else:
                line1 = f"│ [IDLE] NO TRACK LOADED.{' ' * max(0, width - 27)}│"

            self._safe_addstr(top + 1, left, line1, curses.A_BOLD)

            # Scrubber Bar
            curr_str = format_duration(curr_sec)
            tot_str = format_duration(tot_sec)
            rem_sec = max(0.0, tot_sec - curr_sec)
            rem_str = f"-{format_duration(rem_sec)}"
            time_pre = f"{curr_str} ["
            time_post = f"] {tot_str} ({rem_str}) {pct:5.1f}%"
            fixed_len = len(time_pre) + len(time_post) + 4
            bar_w = max(6, width - fixed_len)

            if tot_sec > 0:
                filled_len = int((curr_sec / tot_sec) * bar_w)
                dither_len = min(2, filled_len)
                solid_len = max(0, filled_len - dither_len)
                empty_len = max(0, bar_w - filled_len)
                bar_chars = ("█" * solid_len) + ("▓" * dither_len) + ("░" * empty_len)
            else:
                bar_chars = "░" * bar_w

            line2 = f"│ {time_pre}{bar_chars}{time_post}{' ' * max(0, width - fixed_len - bar_w)} │"
            self._safe_addstr(top + 2, left, line2, curses.A_NORMAL)

            # Mode / Volume bar
            now = time.time()
            if self.is_filtering:
                status_text = f"QUERY: {self.filter_query}_ [ENTER/ESC]"
            elif self.status_message and (now - self.status_time < 3.0):
                status_text = f"STATUS >> {self.status_message}"
            else:
                vol_lvl = int(self.audio.volume * 10)
                vol_bar = ("█" * vol_lvl) + ("░" * (10 - vol_lvl))
                vol_str = f"VOL:[{vol_bar}]"
                rep_str = f"TRACK LOOP:{'ON' if self.loop_current_track else 'OFF'}"
                queue_rep = f"QUEUE:{self.queue.repeat_mode.name}"
                cover_hint = "[V] COVER"
                status_text = f"{vol_str} │ {rep_str} │ {queue_rep} │ {cover_hint}"

            self._safe_addstr(top + 3, left, "└" + ("─" * (width - 2)) + "┘", curses.A_NORMAL)
            self._safe_addstr(top + 3, left + 3, f" {status_text} ", curses.A_BOLD)

    def _render_cover_modal(self, h: int, w: int) -> None:
        """Render high-resolution 1-bit pixel art cover modal directly in the CLI."""
        track = self.cover_modal_track
        if not track:
            return

        box_w = min(84, w - 2)
        box_h = min(24, h - 2)
        top = max(0, (h - box_h) // 2)
        left = max(0, (w - box_w) // 2)

        for r in range(box_h):
            self._safe_addstr(top + r, left, " " * box_w, curses.color_pair(1))

        self._safe_addstr(top, left, "┌" + ("─" * (box_w - 2)) + "┐", curses.A_BOLD)
        header_title = f" ■ ALBUM ART // {track.title[:30].upper()} "
        self._safe_addstr(top + 1, left, "│ " + header_title + (" " * max(0, box_w - 4 - len(header_title))) + " │", curses.color_pair(2) | curses.A_BOLD)
        self._safe_addstr(top + 2, left, "├" + ("─" * (box_w - 2)) + "┤", curses.A_BOLD)

        inner_h = box_h - 4
        art_w = min(36, max(14, int(box_w * 0.44)))
        lines = track.get_pixel_art(art_w, inner_h, algorithm=self.dither_algo)

        status_notice = "• 1-BIT MONOCHROME CLI RENDER" if track.has_cover else "• NO COVER ATTACHED"
        src_map = {
            "embedded": "SOURCE: EMBEDDED IN AUDIO TAGS",
            "folder": "SOURCE: LOCAL ALBUM FOLDER ART",
            "online": "SOURCE: ITUNES MUSIC CATALOG",
        }
        source_notice = src_map.get(track.cover_source, "SOURCE: NONE")

        details = [
            f"TRACK:   {track.title}",
            f"ARTIST:  {track.artist}",
            f"ALBUM:   {track.album}" + (f" ({track.year})" if track.year else ""),
            f"GENRE:   {track.genre}" if track.genre else "",
            f"FORMAT:  {track.file_format} Lossless ({track.bits_per_sample}-bit)" if track.file_format == "FLAC" else f"FORMAT:  {track.file_format}",
            f"SPECS:   {track.sample_rate} Hz / {track.channels}",
            f"BITRATE: {track.bitrate} kbps",
            source_notice,
            status_notice,
            "[ PRESS ANY KEY OR V TO CLOSE ]",
        ]
        active_details = [d for d in details if d != ""]

        for idx in range(inner_h):
            row_y = top + 3 + idx
            art_line = lines[idx] if idx < len(lines) else (" " * art_w)
            det_line = active_details[idx] if idx < len(active_details) else ""
            avail_det = max(0, box_w - 4 - art_w - 3)

            self._safe_addstr(row_y, left, "│ ", curses.A_BOLD)
            self._safe_addstr(row_y, left + 2, art_line, curses.A_NORMAL)
            self._safe_addstr(row_y, left + 2 + art_w, " │ ", curses.A_DIM)
            is_highlight = "PRESS" in det_line or "CLI RENDER" in det_line
            self._safe_addstr(row_y, left + 2 + art_w + 3, f"{det_line:<{avail_det}}"[:avail_det], curses.A_BOLD if is_highlight else curses.A_NORMAL)
            self._safe_addstr(row_y, left + box_w - 1, "│", curses.A_BOLD)

        self._safe_addstr(top + box_h - 1, left, "└" + ("─" * (box_w - 2)) + "┘", curses.A_BOLD)

    def _render_footer(self, y: int, width: int) -> None:
        """High-contrast solid pixel command bar."""
        clear_label = "HISTORY" if self.active_pane == 2 else "QUEUE" if self.active_pane == 1 else ""
        clear_hint = f" [C] CLEAR {clear_label}" if clear_label else ""
        cmd_text = f" [SPACE] PLAY  [L] LOOP  [TAB] PANE  [1/2/3] SELECT{clear_hint}  [+/-] VOL  [?] HELP  [Q] QUIT "
        self._safe_addstr(y, 0, " " * width, curses.color_pair(2))
        self._safe_addstr(y, 0, cmd_text[:width], curses.color_pair(2) | curses.A_BOLD)

    def _render_help_screen(self, h: int, w: int) -> None:
        box_w = min(76, w - 4)
        box_h = min(26, h - 4)
        top = (h - box_h) // 2
        left = (w - box_w) // 2

        for r in range(box_h):
            self._safe_addstr(top + r, left, " " * box_w, curses.color_pair(2))

        lines = [
            "■ PULSE // KEYBOARD MATRIX",
            "─────────────────────────────────────────────────────────────",
            "PLAYBACK & AUDIO DSP:",
            "  SPACE          TOGGLE PLAY / PAUSE",
            "  LEFT / RIGHT   SEEK -5S / +5S (CLICKLESS RAMPED)",
            "  + / -          VOLUME UP / DOWN (5%)",
            "  M              MUTE / UNMUTE",
            "  N / P          NEXT / PREV TRACK",
            "  R              CYCLE QUEUE REPEAT (OFF -> TRACK -> QUEUE)",
            "  L              TOGGLE LOOP FOR CURRENT SONG",
            "  S              TOGGLE SHUFFLE (BIJECTIVE PERMUTATION)",
            "  T              CYCLE DITHER ALGORITHM (ATKINSON -> FLOYD-S -> BAYER)",
            "  3              OPEN LISTENING HISTORY",
            "  G              TOGGLE REPLAYGAIN LOUDNESS NORMALIZATION",
            "  V              OPEN COVER ART MODAL",
            "  C              CLEAR LISTENING HISTORY (WHEN PANE 3 IS ACTIVE)",
            "",
            "NAVIGATION & LIBRARY:",
            "  J / K (UP/DN)  MOVE SELECTION",
            "  ENTER          OPEN DIR / PLAY FILE",
            "  ENTER          PLAY SELECTED HISTORY ENTRY (PANE 3)",
            "  BACKSPACE / H  ASCEND (PARENT DIRECTORY)",
            "  A              QUEUE FILE OR ENTIRE DIR",
            "  TAB            CYCLE PANE (FILES -> QUEUE -> LISTENING HISTORY)",
            "  1 / 2 / 3      SELECT PANE DIRECTLY (1:FILES, 2:QUEUE, 3:HISTORY)",
            "  /              SEARCH SONG FILENAMES IN THE FILES PANE",
            "  D / X          REMOVE TRACK FROM QUEUE",
            "  C              CLEAR ALL QUEUE ITEMS",
            "  Q              QUIT",
            "─────────────────────────────────────────────────────────────",
            "PRESS ANY KEY TO CLOSE...",
        ]

        for idx, line in enumerate(lines[:box_h - 2]):
            self._safe_addstr(top + 1 + idx, left + 2, line, curses.color_pair(2) | curses.A_BOLD)
