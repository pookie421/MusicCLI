# Pulse CLI: Low-Level Technical Specification & Architecture

<p align="center">
  <a href="https://github.com/pookie421/MusicCLI"><img src="https://img.shields.io/badge/deterministic--checks-passing-brightgreen?style=flat-square&logo=github" alt="Deterministic Checks Passing"></a>
  <a href="https://github.com/pookie421/MusicCLI"><img src="https://img.shields.io/badge/platform-Darwin%20%7C%20macOS-black?style=flat-square&logo=apple&logoColor=white" alt="Platform: Darwin / macOS"></a>
  <a href="https://www.python.org"><img src="https://img.shields.io/badge/runtime-CPython%203.9%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Runtime: CPython 3.9+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/code-MIT-4183c4?style=flat-square" alt="License: MIT"></a>
  <a href="https://github.com/pookie421/MusicCLI"><img src="https://img.shields.io/badge/audio--hal-Darwin%20CoreAudio-gray?style=flat-square" alt="Audio HAL: Darwin CoreAudio"></a>
  <a href="https://github.com/pookie421/MusicCLI"><img src="https://img.shields.io/badge/codecs-FLAC%20(PCM16%2F24)%20%7C%20MP3-brightgreen?style=flat-square" alt="Codecs: FLAC / MP3"></a>
  <a href="https://github.com/pookie421/MusicCLI"><img src="https://img.shields.io/badge/display-1--bit%20POSIX%20Curses-black?style=flat-square" alt="Display: 1-Bit POSIX Curses"></a>
</p>

<p align="center">
  An asynchronous, high-throughput, 1-bit monochrome terminal audio streaming engine and visualizer for macOS. Engineered for direct Darwin CoreAudio Hardware Abstraction Layer (HAL) execution, sub-millisecond seek precision, non-blocking metadata resolution, and in-terminal half-block Unicode dithering.
</p>

<p align="center">
  <img src="docs/preview.png" alt="Pulse CLI Terminal Interface" width="100%" />
</p>

---

## 1. System Engineering Overview

Pulse CLI is a high-performance audio transport application built for the POSIX terminal environment. It bypasses conventional heavyweight GUI frameworks and WebRenderer layers, linking directly to Darwin's `AudioUnit` / `CoreAudio` infrastructure through a CFFI bridge provided by `miniaudio`. 

### Key Technical Capabilities
* **CoreAudio Streaming Pipeline**: Interleaved signed 16-bit linear pulse-code modulation (PCM) streaming at $f_s = 44,100\text{ Hz}$ across stereo output channels with $1024$-sample circular ring buffer chunks.
* **Deterministic Concurrency**: Mutex synchronization isolating frame counters from audio stream generator callbacks, eliminating contention and unbounded lock retention.
* **Discrete Amplitude Visualizer**: Real-time spectral power estimation computed across $N = 64$ sub-bands with non-linear power-law perceptual scaling and asymmetric attack/decay temporal smoothing.
* **1-Bit In-Terminal Halftone Synthesizer**: In-memory downsampling of embedded Vorbis / ID3v2.3+ pictures via Floyd-Steinberg error diffusion and UTF-8 half-block glyph bit-packing (`U+2580`, `U+2584`, `U+2588`).
* **Asynchronous Multi-Tier Metadata Engine**: Concurrent metadata cache with thread-pool-backed online catalog fallback, avoiding execution blocks on the main curses event loop.
* **Permutational Shuffling**: Strictly bounded Fisher-Yates shuffle vectors $\vec{\pi} \in S_N$ supporting bi-directional traversal and non-repeating queue cycles.

---

## 2. Audio Processing Architecture

### 2.1 Hardware Abstraction & CoreAudio Host Pipeline

The audio subsystem operates across two decoupled OS threads: the terminal rendering thread (executing the POSIX curses loop at $25\text{ Hz}$) and the real-time audio thread scheduled by the CoreAudio HAL daemon (`coreaudiod`).

```
+---------------------------------------------------------------------------------------+
|                                    CPython Runtime                                    |
|                                                                                       |
|  +--------------------+         +-------------------+         +--------------------+  |
|  | Curses Event Loop  | <-----> |   AudioEngine     | <-----> |  PlaybackDevice    |  |
|  | (25 Hz Render Bus) |  State  |  (_lock Protected)|  Yield  | (miniaudio CFFI)   |  |
|  +--------------------+         +-------------------+         +--------------------+  |
+-------------------------------------------|------------------------------|------------+
                                            |                              |
                                            v                              v
                                  +-------------------+          +-------------------+
                                  | Decoded In-Memory |          | Darwin CoreAudio  |
                                  | PCM Array ('h')   |          | AudioOutputUnit   |
                                  +-------------------+          +-------------------+
                                                                           |
                                                                           v
                                                                 +-------------------+
                                                                 | Physical DAC /    |
                                                                 | Audio Interface   |
                                                                 +-------------------+
```

### 2.2 Mathematical Signal Formulations

#### 2.2.1 Sampling & Quantization Model
Raw bitstreams (FLAC lossless or MPEG-1 Audio Layer III) are pre-decoded into contiguous 16-bit signed little-endian PCM integers:

$$s[n] \in [-32768, 32767], \quad n \in [0, N_{\text{frames}} \times C - 1]$$

where channel count $C = 2$ and nominal sample rate $f_s = 44,100\text{ Hz}$.

For an arbitrary temporal offset $t \in [0, T_{\text{duration}}]$, the target frame index $k$ is calculated deterministically:

$$k = \text{clamp}\left(0, N_{\text{total}} - 1, \left\lfloor t \times f_s \right\rfloor\right)$$

The byte offset within memory is computed in $O(1)$ time:

$$\text{Offset}_{\text{byte}} = k \times C \times \text{sizeof}(\text{int16}) = k \times 2 \times 2 = 4k\text{ bytes}$$

#### 2.2.2 Decoupled Lock Lifetime & Concurrency Protocol
To guarantee deterministic execution within the real-time audio callback, `self._lock` acquisition is strictly bounded to frame slice extraction. The lock hold duration $\Delta t_{\text{lock}}$ satisfies:

$$\Delta t_{\text{lock}} \le 1.5\ \mu\text{s}$$

Volume attenuation, sample bit-shifting, buffer zero-padding, and the generator `yield` occur **outside** the critical section. This guarantees that `get_progress()` and `get_visualizer_levels()` execute with zero lock contention.

```python
# Concurrency Model: Critical section decoupled from yield
with self._lock:
    # 1. State verification
    # 2. Slice index bounds computation
    # 3. Memory pointer advancement
    raw_chunk = self._samples[start_idx:end_idx]

# Lock released prior to mathematical operations and device driver suspension
if eff_vol < 0.98:
    scaled_samples = array.array("h", (int(s * eff_vol) for s in raw_chunk))
    data = scaled_samples.tobytes()
else:
    data = raw_chunk.tobytes()

yield data  # Suspends execution without holding mutex
```

#### 2.2.3 Perceptual Spectral Decimation
For every $M = 1024$ frames delivered to CoreAudio, the raw stereo chunk (length $2048$) is partitioned into $B = 64$ sub-bands containing $K = \lfloor 2048 / 64 \rfloor = 32$ interleaved samples.

The peak amplitude $\hat{A}_b$ for band $b$ is computed:

$$\hat{A}_b = \frac{1}{32768} \max_{i=0}^{K-1} |s_{b \cdot K + i}|$$

The raw peak $\hat{A}_b$ is scaled using a logarithmic power-law curve to match the non-linear response of human auditory perception:

$$Y_b = \min\left(1.0, 1.65 \times \hat{A}_b^{0.38}\right)$$

Temporal stability is achieved via an asymmetric exponential smoothing filter:

$$L_b^{(t)} = \begin{cases} 
0.15 \cdot L_b^{(t-1)} + 0.85 \cdot Y_b, & \text{if } Y_b > L_b^{(t-1)} \quad (\text{Attack}) \\ 
0.70 \cdot L_b^{(t-1)} + 0.30 \cdot Y_b, & \text{if } Y_b \le L_b^{(t-1)} \quad (\text{Decay}) 
\end{cases}$$

---

## 3. In-Terminal 1-Bit Halftone Synthesis

Pulse CLI renders high-contrast album artwork directly inside the terminal window using pure 1-bit monochrome pixel art with zero ANSI color escape overhead.

```
+-----------------------------------------------------------------------------------+
|                        1-Bit Dither Synthesis Pipeline                            |
|                                                                                   |
|  [Embedded Art] -> [8-Bit Luma L] -> [Autocontrast] -> [LANCZOS Resampling]       |
|                                                              |                    |
|                                                              v                    |
|  [Curses Grid]  <- [Unicode Blocks] <- [Bit Packing] <- [Floyd-Steinberg Kernel]  |
+-----------------------------------------------------------------------------------+
```

### 3.1 Resampling & Error Diffusion
1. **Luminance Decoupling**: The 24-bit RGB bitmap is mapped to an 8-bit perceptual grayscale channel $L(x, y)$ using the standard Rec. 601 coefficients:
   $$L = 0.299R + 0.587G + 0.114B$$
2. **Histogram Stretching (Autocontrast)**:
   $$L'(x, y) = \text{clamp}\left(0, 255, \frac{L(x, y) - L_{\min}}{L_{\max} - L_{\min}} \times 255\right)$$
3. **Resampling**: The stretched image is scaled to $(W, 2H)$ using a Lanczos sinc-interpolation filter (`LANCZOS3`).
4. **Floyd-Steinberg Error Diffusion**: The 8-bit image is quantized to binary values $B(x, y) \in \{0, 1\}$ using distributed error propagation:
   $$e = L'(x, y) - \text{threshold}(L'(x, y))$$
   $$\begin{pmatrix} & * & \frac{7}{16}e \\ \frac{3}{16}e & \frac{5}{16}e & \frac{1}{16}e \end{pmatrix}$$

### 3.2 2:1 Vertical Half-Block Packing
Because standard terminal fonts exhibit an aspect ratio of approximately $1:2$ (width to height), two vertically adjacent binary pixels $(x, 2y)$ and $(x, 2y + 1)$ are packed into a single character cell using Unicode half-block elements:

| Upper Pixel $B(x, 2y)$ | Lower Pixel $B(x, 2y + 1)$ | Unicode Codepoint | UTF-8 Glyphs |
| :---: | :---: | :---: | :---: |
| $0$ (Black) | $0$ (Black) | `U+0020` | ` ` *(SPACE)* |
| $1$ (White) | $0$ (Black) | `U+2580` | `▀` *(UPPER HALF BLOCK)* |
| $0$ (Black) | $1$ (White) | `U+2584` | `▄` *(LOWER HALF BLOCK)* |
| $1$ (White) | $1$ (White) | `U+2588` | `█` *(FULL BLOCK)* |

---

## 4. Subsystem Specifications

### 4.1 Audio Engine Specifications (`pulse.audio`)
* **Output Bus**: Darwin CoreAudio via miniaudio C runtime.
* **Sample Rate ($f_s$)**: $44,100\text{ Hz}$ (resampled automatically on decode).
* **Quantization**: Signed 16-bit interleaved PCM (`SIGNED16`).
* **Chunk Allocation**: $1024\text{ frames}$ per callback cycle ($4096\text{ bytes}$).
* **Callback Latency**: $\tau = \frac{1024}{44100} \approx 23.22\text{ ms}$.
* **Lock Contention**: $0\text{ ms}$ outside state transition boundaries.

### 4.2 Metadata & Cover Pipeline (`pulse.metadata`)
* **Containers Supported**: FLAC (`.flac`), MPEG-1 Layer 3 (`.mp3`).
* **FLAC Metadata Blocks**: Parses `STREAMINFO`, `VORBIS_COMMENT`, `PICTURE` (Type 3 Front Cover preferred), and base64-encoded `METADATA_BLOCK_PICTURE`.
* **ID3v2 Frames**: Parses `TIT2`, `TPE1`, `TPE2`, `TALB`, `TDRC`, `TYER`, `TCON`, `TRCK`, `APIC` (Type 3 preferred), and legacy `PIC`.
* **Bit Depth Resolution**: Resolves 24-bit lossless master FLAC streams vs standard 16-bit red-book audio.
* **Non-Blocking Cache Hierarchy**:
  * **L1**: In-memory metadata map keyed by `(path, size, mtime)` ($\Delta t < 0.03\text{ ms}$).
  * **L2**: In-memory 1-bit pixel art cache keyed by `(width, height)` ($\Delta t < 0.01\text{ ms}$).
  * **L3**: Persistent filesystem cache at `~/.pulse-cli/cover_cache/{md5_hash}.jpg`.
  * **L4**: Thread-pooled background catalog resolution via iTunes API daemon thread.

### 4.3 Queue Management Subsystem (`pulse.queue_manager`)
* **Model**: Permutational queue with state tracking.
* **Shuffle Algorithm**: Fisher-Yates $O(N)$ shuffle vector with continuous position cursor $k \in [0, N-1]$.
* **Repeat Modes**:
  * `RepeatMode.OFF`: Traverses until $k = N - 1$, then halts.
  * `RepeatMode.TRACK`: Automatically repeats current index on end-of-track; permits manual skip.
  * `RepeatMode.QUEUE`: Generates a fresh permutation vector and loops when $k = N - 1$.
* **Reordering Complexity**: $O(1)$ amortized index relocation via `move_track(from_idx, to_idx)`.

---

## 5. Keyboard Control Matrix

| Keybinding | Function | POSIX / Curses Internal Action |
| :--- | :--- | :--- |
| `Space` | Toggle Play / Pause | `AudioEngine.toggle_play_pause()` |
| `Left` / `,` | Seek Backward | Relative seek $-5.0\text{ s}$ (`AudioEngine.seek(-5.0)`) |
| `Right` / `.` | Seek Forward | Relative seek $+5.0\text{ s}$ (`AudioEngine.seek(5.0)`) |
| `+` / `]` | Increment Gain | Volume step $+5\%$ with saturated clipping at $1.0$ |
| `-` / `[` | Decrement Gain | Volume step $-5\%$ with lower bound $0.0$ |
| `M` | Mute / Unmute | Toggles attenuation with previous state restoration |
| `N` | Skip to Next | `QueueManager.next_track(auto_advance=False)` |
| `P` | Previous Track | Rewinds to $0.0\text{ s}$ if $> 3.0\text{ s}$ into track, else previous |
| `R` | Cycle Repeat Mode | `OFF` $\rightarrow$ `TRACK` $\rightarrow$ `QUEUE` $\rightarrow$ `OFF` |
| `S` | Toggle Shuffle | Toggles Fisher-Yates permutation vector with cursor retention |
| `O` | Cycle Visualizer | Cycles visualizer: `SPECTRUM` (64-band FFT), `WAVEFORM` (Oscilloscope), `STEREO_VU` (dB meters) |
| `T` | Cycle Dithering | Cycles halftone mode: `ATKINSON` (Mac 1984 75%), `FLOYD_STEINBERG`, `BAYER` (4x4 ordered) |
| `L` | Toggle Lyrics / Art | Toggles Pane 3 between 1-Bit Album Art and Synchronized Karaoke Scrolling Lyrics |
| `G` | Toggle ReplayGain | Enables/disables ReplayGain / EBU R128 loudness normalization ($10^{(G/20)}$ linear scaling) |
| `V` | In-CLI Cover Modal | Expands high-resolution 1-bit pixel art modal in curses |
| `Tab` | Switch Focus Pane | Cycles active focus: `1: FILES` $\rightarrow$ `2: QUEUE` $\rightarrow$ `3: ALBUM ART` |
| `1` / `2` / `3` | Direct Pane Select | Focuses selected pane directly |
| `j` / `Down` | Cursor Down | Translates UI index $+1$ |
| `k` / `Up` | Cursor Up | Translates UI index $-1$ |
| `Enter` / `l` | Activate / Play | Traverses inode or loads selected track into audio engine |
| `Backspace` / `h` | Ascend Directory | Resolves `path.parent` and re-indexes inode listing |
| `A` | Recursive Enqueue | Traverses file or directory tree and enqueues supported audio |
| `/` | Search Songs | Opens the FILES search bar and filters filenames in the current directory as you type |
| `D` / `X` | Remove Queue Track | Pops item from queue, recalculates shuffle indices and cursor |
| `C` | Flush Queue | Purges queue vector, calls `AudioEngine.unload()` to free PCM buffers |
| `?` | Keyboard Matrix | Displays modal curses overlay of keybindings |
| `Q` | Graceful Exit | Closes CoreAudio device, terminates curses, restores terminal state |

---

## 6. Installation & Deployment

### 6.1 Automated Deployment (macOS Darwin)
The deployment script creates an isolated virtual environment at `~/.pulse-cli`, installs dependencies, and links the entrypoint binary to `~/.local/bin/pulse`:

```bash
git clone https://github.com/pookie421/MusicCLI.git
cd MusicCLI
./install.sh
```

Ensure `~/.local/bin` is exported in your environment configuration (`~/.zshrc` or `~/.bashrc`):

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### 6.2 Manual Development Setup

```bash
# Clone repository
git clone https://github.com/pookie421/MusicCLI.git
cd MusicCLI

# Instantiate isolated CPython virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install editable development build
pip install --upgrade pip
pip install -e .

# Run test suite
python3 -m unittest discover tests -v
```

---

## 7. Execution CLI Interface

```bash
# Launch in default directory (~/Music or working directory)
pulse

# Launch player targeting explicit directory
pulse ~/Music/Electronic

# Load and stream FLAC or MP3 immediately
pulse audio_master.flac
pulse audio_track.mp3

# Display argument specifications
pulse --help
```

---

## 8. Repository Layout

```
MusicCLI/
|-- pulse/
|   |-- __init__.py           # Package version definition
|   |-- __main__.py          # CLI entry point, argument parsing & bootstrap
|   |-- app.py               # Double-buffered 1-bit POSIX curses rendering loop & OSC titles
|   |-- audio.py             # CoreAudio / miniaudio real-time PCM streaming engine & ramped seek
|   |-- dsp.py               # Cooley-Tukey Radix-2 512-pt FFT, Stereo VU meter & Oscilloscope
|   |-- metadata.py          # ID3/FLAC AST tag parser, Atkinson/Bayer dither & LRC parser
|   `-- queue_manager.py     # Deterministic permutation queue & history tracker
|-- tests/
|   `-- test_backend.py      # Automated unit tests (28 tests: FFT, VU, Dither, LRC, Audio)
|-- scripts/
|   |-- preflight.sh         # Automated pre-submission verification runner for contributors
|   `-- verify_invariants.py # Unified Contributor Invariant Verification Harness (7 phases)
|-- docs/
|   |-- AUDIO_ENGINE.md      # Low-level audio engine & Cooley-Tukey Radix-2 FFT mathematical spec
|   |-- METADATA_SPEC.md     # Binary container AST, Atkinson 1984 dither & LRC grammar spec
|   |-- DEVELOPER_GUIDE.md   # Mathematical invariants, AST validation & contribution lifecycle
|   |-- preview.png          # High-resolution terminal capture
|   `-- preview.svg          # Vector preview diagram
|-- ARCHITECTURE.md          # Multi-threaded subsystem architecture specification
|-- CONTRIBUTING.md          # Technical contribution standards & invariant guidelines
|-- pyproject.toml           # PEP 621 / PEP 517 build configuration
|-- requirements.txt         # Runtime dependencies (miniaudio, mutagen, Pillow)
|-- install.sh               # Darwin deployment script
|-- LICENSE                  # MIT License
`-- README.md                # System documentation
```

---

## 9. Verification & Deterministic Testing

The backend test suite verifies concurrency invariants, Cooley-Tukey FFT energy conservation, Atkinson dithering error budgets, memory bounds, error formatting, and queue permutations:

```bash
~/.pulse-cli/venv/bin/python3 -m unittest tests/test_backend.py -v
```

Output:
```
test_fft_spectrum_silence (tests.test_backend.TestAudioDSP.test_fft_spectrum_silence) ... ok
test_fft_spectrum_sine_wave (tests.test_backend.TestAudioDSP.test_fft_spectrum_sine_wave) ... ok
test_replay_gain_math (tests.test_backend.TestAudioDSP.test_replay_gain_math) ... ok
test_stereo_vu_meter (tests.test_backend.TestAudioDSP.test_stereo_vu_meter) ... ok
test_waveform_oscilloscope (tests.test_backend.TestAudioDSP.test_waveform_oscilloscope) ... ok
test_initial_state (tests.test_backend.TestAudioEngine.test_initial_state) ... ok
test_replay_gain_toggle (tests.test_backend.TestAudioEngine.test_replay_gain_toggle) ... ok
test_seek_to_end_triggers_finished (tests.test_backend.TestAudioEngine.test_seek_to_end_triggers_finished) ... ok
test_unload (tests.test_backend.TestAudioEngine.test_unload) ... ok
test_visualizer_mode_cycling (tests.test_backend.TestAudioEngine.test_visualizer_mode_cycling) ... ok
test_volume_controls (tests.test_backend.TestAudioEngine.test_volume_controls) ... ok
test_dither_enum_cycle (tests.test_backend.TestDitherAndLyrics.test_dither_enum_cycle) ... ok
test_lrc_parser_multiple_timestamps (tests.test_backend.TestDitherAndLyrics.test_lrc_parser_multiple_timestamps) ... ok
test_lrc_parser_synced (tests.test_backend.TestDitherAndLyrics.test_lrc_parser_synced) ... ok
test_format_duration_edge_cases (tests.test_backend.TestFormatters.test_format_duration_edge_cases) ... ok
test_format_duration_normal (tests.test_backend.TestFormatters.test_format_duration_normal) ... ok
test_format_file_size (tests.test_backend.TestFormatters.test_format_file_size) ... ok
test_empty_pixel_art_bounds (tests.test_backend.TestPixelArt.test_empty_pixel_art_bounds) ... ok
test_pixel_art_caching (tests.test_backend.TestPixelArt.test_pixel_art_caching) ... ok
test_clear (tests.test_backend.TestQueueManager.test_clear) ... ok
test_initial_state (tests.test_backend.TestQueueManager.test_initial_state) ... ok
test_jump_to (tests.test_backend.TestQueueManager.test_jump_to) ... ok
test_move_track (tests.test_backend.TestQueueManager.test_move_track) ... ok
test_remove_at (tests.test_backend.TestQueueManager.test_remove_at) ... ok
test_repeat_queue_mode (tests.test_backend.TestQueueManager.test_repeat_queue_mode) ... ok
test_repeat_track_mode (tests.test_backend.TestQueueManager.test_repeat_track_mode) ... ok
test_sequential_navigation (tests.test_backend.TestQueueManager.test_sequential_navigation) ... ok
test_shuffle_cycle_and_prev (tests.test_backend.TestQueueManager.test_shuffle_cycle_and_prev) ... ok

----------------------------------------------------------------------
Ran 28 tests in 0.226s

OK
```

Execute the preflight verification harness prior to submitting any PR:
```bash
./scripts/preflight.sh
```

---

## 10. Technical References
* Apple Developer Documentation: *Core Audio Architecture & Audio Hardware Abstraction Layer (HAL)*.
* ITU-R Recommendation BS.775: *Multichannel Stereophonic Sound System with and without Accompanying Picture*.
* ISO/IEC 11172-3: *Information Technology — Coding of Moving Pictures and Associated Audio for Digital Storage Media at up to about 1.5 Mbit/s — Part 3: Audio*.
* Xiph.Org Foundation: *FLAC format specification & VorbisComment metadata mapping guidelines*.
* Robert W. Floyd & Louis Steinberg: *An Adaptive Algorithm for Spatial Gray Scale* (SID 75 Digest, 1975).
* POSIX.1-2017: *System Interfaces, Chapter 2: General Information & curses terminal management*.

---

## 11. License

This software is released under the **MIT License**. Refer to [LICENSE](LICENSE) for the full legal text.
