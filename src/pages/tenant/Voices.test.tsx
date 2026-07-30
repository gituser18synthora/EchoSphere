import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Voices from "@/pages/tenant/Voices";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  listVoiceClones: vi.fn(),
  getVoiceCloneConfig: vi.fn(),
  createVoiceClone: vi.fn(),
  updateVoiceClone: vi.fn(),
  setVoiceCloneStatus: vi.fn(),
  deleteVoiceClone: vi.fn(),
  generateTtsPreview: vi.fn(),
  listProviderModels: vi.fn(),
  getModelLanguages: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const listVoiceClones = vi.mocked(api.listVoiceClones);
const getVoiceCloneConfig = vi.mocked(api.getVoiceCloneConfig);
const createVoiceClone = vi.mocked(api.createVoiceClone);
const deleteVoiceClone = vi.mocked(api.deleteVoiceClone);

const CONFIG = {
  providers: [
    {
      code: "elevenlabs", name: "ElevenLabs", supportsCloning: true, hasCredentials: true,
      cloneParams: [
        { name: "description", type: "string" as const, label: "Description", optional: true, maxLength: 500 },
        { name: "removeBackgroundNoise", type: "boolean" as const, label: "Remove background noise", default: false, optional: true },
      ],
      reason: null,
    },
    {
      code: "sarvam", name: "Sarvam AI", supportsCloning: false, hasCredentials: true,
      cloneParams: [],
      reason: "Sarvam offers voice cloning only inside Sarvam Studio (in-browser recording, beta) — its public API has no voice-cloning endpoint.",
    },
  ],
  allowedExtensions: ["mp3", "wav", "webm"],
  accept: ".mp3,.wav,.webm",
  maxFiles: 10,
  maxFileMb: 10,
  maxTotalMb: 30,
};

const CLONE = {
  id: "vp_clone1",
  tenantId: "tn-001",
  source: "cloned" as const,
  cloneMetadata: { samples: [{ fileName: "s.wav", sizeBytes: 2048 }] },
  name: "Support Narrator",
  gender: "female" as const,
  languages: [],
  accent: "",
  styles: [],
  description: "warm narrator",
  latencyMs: 0,
  premium: false,
  sample: "",
  provider: "elevenlabs",
  providerVoiceId: "pv_abc123",
  status: "active",
  modelCodes: ["eleven_flash_v2_5"],
  usageCount: 0,
};

function installMocks(voices: (typeof CLONE)[] = []) {
  listVoiceClones.mockResolvedValue(voices as never);
  getVoiceCloneConfig.mockResolvedValue(CONFIG as never);
  createVoiceClone.mockResolvedValue({ ...CLONE, id: "vp_new", name: "New Voice" } as never);
  deleteVoiceClone.mockResolvedValue({ deleted: true, providerDeleted: true } as never);
  vi.mocked(api.listProviderModels).mockResolvedValue([
    {
      code: "eleven_flash_v2_5", displayName: "Eleven Flash v2.5", provider: "elevenlabs",
      capability: "tts", languages: [], codecs: [], sampleRates: [], streaming: true,
      paramsSchema: {}, isDefault: true,
    },
  ] as never);
  vi.mocked(api.getModelLanguages).mockResolvedValue({
    languages: [{ code: "en-IN", name: "English (India)", nativeName: null }],
    supportsAutoDetect: false, languageAgnostic: true,
  } as never);
}

async function openCloneModal(user: ReturnType<typeof userEvent.setup>) {
  render(<Voices />);
  await user.click((await screen.findAllByRole("button", { name: "Clone Voice" }))[0]);
  return screen.findByRole("dialog");
}

describe("Tenant Voices page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installMocks();
  });

  it("shows the empty state with the Clone Voice action", async () => {
    render(<Voices />);
    expect(await screen.findByText("No cloned voices yet")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Clone Voice" }).length).toBeGreaterThan(0);
  });

  it("lists cloned voices with provider, status and actions", async () => {
    installMocks([CLONE]);
    render(<Voices />);
    expect(await screen.findByText("Support Narrator")).toBeInTheDocument();
    expect(screen.getByText("Cloned")).toBeInTheDocument();
    expect(screen.getByText("elevenlabs")).toBeInTheDocument();
    expect(screen.getByText("pv_abc123")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /preview/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /deactivate/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /delete support narrator/i })).toBeInTheDocument();
  });

  it("clones a voice: provider-driven fields, sample upload, multipart submit", async () => {
    const user = userEvent.setup();
    const dialog = await openCloneModal(user);
    // ElevenLabs (cloning-capable) preselected, its provider-specific fields render.
    expect(within(dialog).getByLabelText("Clone provider")).toHaveValue("elevenlabs");
    expect(within(dialog).getByText("Remove background noise")).toBeInTheDocument();

    await user.type(within(dialog).getByPlaceholderText(/support narrator/i), "My Narrator");
    const file = new File([new Uint8Array([82, 73, 70, 70, 1, 2, 3, 4])], "sample.wav", { type: "audio/wav" });
    await user.upload(within(dialog).getByLabelText("Choose audio samples"), file);
    expect(await within(dialog).findByText("sample.wav")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Clone Voice" }));
    await waitFor(() => expect(createVoiceClone).toHaveBeenCalledTimes(1));
    const form = createVoiceClone.mock.calls[0][0] as FormData;
    expect(form.get("provider")).toBe("elevenlabs");
    expect(form.get("name")).toBe("My Narrator");
    expect(form.get("removeBackgroundNoise")).toBe("false");
    expect((form.getAll("files")[0] as File).name).toBe("sample.wav");
    // List refreshes after a successful clone.
    await waitFor(() => expect(listVoiceClones).toHaveBeenCalledTimes(2));
  });

  it("rejects unsupported sample types client-side", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const dialog = await openCloneModal(user);
    await user.type(within(dialog).getByPlaceholderText(/support narrator/i), "X");
    const bad = new File([new Uint8Array([1])], "notes.txt", { type: "text/plain" });
    await user.upload(within(dialog).getByLabelText("Choose audio samples"), bad);
    expect(await within(dialog).findByText(/unsupported file type/i)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Clone Voice" })).toBeDisabled();
  });

  it("explains that Sarvam has no cloning API instead of faking it", async () => {
    const user = userEvent.setup();
    const dialog = await openCloneModal(user);
    await user.selectOptions(within(dialog).getByLabelText("Clone provider"), ["sarvam"]);
    expect(await within(dialog).findByText(/does not support voice cloning/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/Sarvam Studio/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Clone Voice" })).toBeDisabled();
  });

  describe("Record Live Voice", () => {
    class FakeMediaRecorder {
      static last: FakeMediaRecorder | null = null;
      static isTypeSupported = (mime: string) => mime.startsWith("audio/webm");
      state = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((e: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: unknown, _opts?: unknown) {
        FakeMediaRecorder.last = this;
      }
      start() { this.state = "recording"; }
      stop() {
        this.state = "inactive";
        this.ondataavailable?.({ data: new Blob([new Uint8Array([26, 69, 223, 163, 1, 2, 3])], { type: "audio/webm" }) });
        this.onstop?.();
      }
    }

    class FakeAudio {
      static instances: FakeAudio[] = [];
      src: string;
      currentTime = 0;
      paused = true;
      onended: (() => void) | null = null;
      constructor(src?: string) {
        this.src = src ?? "";
        FakeAudio.instances.push(this);
      }
      play() { this.paused = false; return Promise.resolve(); }
      pause() { this.paused = true; }
    }

    const trackStop = vi.fn();
    const getUserMedia = vi.fn();

    beforeEach(() => {
      vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
      vi.stubGlobal("Audio", FakeAudio);
      FakeAudio.instances = [];
      URL.createObjectURL = vi.fn(() => "blob:mock-recording");
      URL.revokeObjectURL = vi.fn();
      trackStop.mockClear();
      getUserMedia.mockReset().mockResolvedValue({ getTracks: () => [{ stop: trackStop }] });
      Object.defineProperty(navigator, "mediaDevices", {
        value: { getUserMedia }, configurable: true,
      });
    });

    afterEach(() => {
      vi.unstubAllGlobals();
      vi.useRealTimers();
    });

    /** userEvent wired to fake timers so recording time can be advanced.
        shouldAdvanceTime keeps RTL's waitFor polling alive under fake timers. */
    const timedUser = () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      return userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
    };

    /** Open the modal in record mode and capture a take of `ms` milliseconds. */
    const recordTake = async (user: ReturnType<typeof userEvent.setup>, ms: number) => {
      const dialog = await openCloneModal(user);
      await user.click(within(dialog).getByRole("button", { name: "Record Live Voice" }));
      await user.click(await within(dialog).findByRole("button", { name: "Start Recording" }));
      expect(await within(dialog).findByText(/Recording…/)).toBeInTheDocument();
      if (ms > 0) act(() => vi.advanceTimersByTime(ms));
      await user.click(within(dialog).getByRole("button", { name: "Stop" }));
      expect(await within(dialog).findByText(/Recording ready/)).toBeInTheDocument();
      return dialog;
    };

    it("records, previews and uses the recording as a clone sample", async () => {
      const user = timedUser();
      const dialog = await recordTake(user, 2000);
      expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
      // Take length and mic release.
      expect(within(dialog).getByText(/0:02/)).toBeInTheDocument();
      expect(trackStop).toHaveBeenCalled();
      expect(within(dialog).getByRole("button", { name: "Play" })).toBeInTheDocument();
      expect(within(dialog).getByRole("button", { name: "Re-record" })).toBeInTheDocument();

      await user.type(within(dialog).getByPlaceholderText(/support narrator/i), "Recorded Voice");
      await user.click(within(dialog).getByRole("button", { name: "Use Recording" }));
      // The recording lands in the sample list exactly like an uploaded file.
      expect(await within(dialog).findByText(/^recording-\d+\.webm$/)).toBeInTheDocument();

      await user.click(within(dialog).getByRole("button", { name: "Clone Voice" }));
      await waitFor(() => expect(createVoiceClone).toHaveBeenCalledTimes(1));
      const form = createVoiceClone.mock.calls[0][0] as FormData;
      const sent = form.getAll("files")[0] as File;
      expect(sent.name).toMatch(/^recording-\d+\.webm$/);
      expect(sent.type).toBe("audio/webm");
    });

    it("previews with play, pause/resume and replay on a single audio element", async () => {
      const user = timedUser();
      const dialog = await recordTake(user, 3000);

      await user.click(within(dialog).getByRole("button", { name: "Play" }));
      expect(FakeAudio.instances).toHaveLength(1);
      const audio = FakeAudio.instances[0];
      expect(audio.src).toBe("blob:mock-recording");
      expect(audio.paused).toBe(false);

      await user.click(within(dialog).getByRole("button", { name: "Pause" }));
      expect(audio.paused).toBe(true);
      // Resume continues the same element instead of restarting.
      await user.click(within(dialog).getByRole("button", { name: "Resume" }));
      expect(audio.paused).toBe(false);
      expect(FakeAudio.instances).toHaveLength(1);

      audio.currentTime = 7;
      await user.click(within(dialog).getByRole("button", { name: "Replay" }));
      expect(audio.currentTime).toBe(0);
      expect(audio.paused).toBe(false);

      // Playback finishing returns the control to "Play".
      act(() => audio.onended?.());
      expect(await within(dialog).findByRole("button", { name: "Play" })).toBeInTheDocument();
    });

    it("deletes a take and returns to idle without starting a new recording", async () => {
      const user = timedUser();
      const dialog = await recordTake(user, 2000);
      await user.click(within(dialog).getByRole("button", { name: "Delete" }));
      expect(await within(dialog).findByRole("button", { name: "Start Recording" })).toBeInTheDocument();
      expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:mock-recording");
      // Delete only discards — recording must not restart by itself.
      expect(getUserMedia).toHaveBeenCalledTimes(1);
    });

    it("blocks unusably short takes until re-recorded", async () => {
      const user = timedUser();
      const dialog = await recordTake(user, 0);
      expect(within(dialog).getByText(/too short/i)).toBeInTheDocument();
      expect(within(dialog).getByRole("button", { name: "Use Recording" })).toBeDisabled();
      expect(within(dialog).getByRole("button", { name: "Re-record" })).toBeEnabled();
    });

    it("supports re-recording a fresh take", async () => {
      const user = timedUser();
      const dialog = await recordTake(user, 1500);
      await user.click(await within(dialog).findByRole("button", { name: "Re-record" }));
      // A new take starts immediately.
      expect(await within(dialog).findByText(/Recording…/)).toBeInTheDocument();
      expect(getUserMedia).toHaveBeenCalledTimes(2);
    });

    it("keeps the recorder outside any <label> so clicks cannot be forwarded", async () => {
      const user = userEvent.setup();
      const dialog = await openCloneModal(user);
      await user.click(within(dialog).getByRole("button", { name: "Record Live Voice" }));
      const start = await within(dialog).findByRole("button", { name: "Start Recording" });
      // Regression guard: inside a <label>, the browser forwards clicks to the
      // first labelable control (the "Upload Audio File" segment) whenever a
      // recorder button unmounts mid-click, kicking the user out of record mode.
      expect(start.closest("label")).toBeNull();
    });

    it("lets samples in the list be played and stopped before submitting", async () => {
      const user = userEvent.setup();
      const dialog = await openCloneModal(user);
      const file = new File([new Uint8Array([82, 73, 70, 70, 1, 2, 3, 4])], "sample.wav", { type: "audio/wav" });
      await user.upload(within(dialog).getByLabelText("Choose audio samples"), file);
      expect(await within(dialog).findByText("sample.wav")).toBeInTheDocument();

      await user.click(within(dialog).getByRole("button", { name: "Play sample.wav" }));
      expect(FakeAudio.instances).toHaveLength(1);
      expect(FakeAudio.instances[0].paused).toBe(false);

      await user.click(within(dialog).getByRole("button", { name: "Stop preview of sample.wav" }));
      expect(FakeAudio.instances[0].paused).toBe(true);
      expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:mock-recording");
      expect(within(dialog).getByRole("button", { name: "Play sample.wav" })).toBeInTheDocument();
    });

    it("shows an actionable message when microphone access is denied", async () => {
      getUserMedia.mockRejectedValue(new DOMException("denied", "NotAllowedError"));
      const user = userEvent.setup();
      const dialog = await openCloneModal(user);
      await user.click(within(dialog).getByRole("button", { name: "Record Live Voice" }));
      await user.click(await within(dialog).findByRole("button", { name: "Start Recording" }));
      expect(await within(dialog).findByText(/microphone access was denied/i)).toBeInTheDocument();
      // Back to idle so the user can retry after granting access.
      expect(within(dialog).getByRole("button", { name: "Start Recording" })).toBeInTheDocument();
    });

    it("explains when the browser cannot record", async () => {
      vi.unstubAllGlobals();
      vi.stubGlobal("MediaRecorder", undefined);
      const user = userEvent.setup();
      const dialog = await openCloneModal(user);
      await user.click(within(dialog).getByRole("button", { name: "Record Live Voice" }));
      await user.click(await within(dialog).findByRole("button", { name: "Start Recording" }));
      expect(await within(dialog).findByText(/not supported in this browser/i)).toBeInTheDocument();
    });
  });

  it("deletes a clone after confirmation", async () => {
    installMocks([CLONE]);
    const user = userEvent.setup();
    render(<Voices />);
    await user.click(await screen.findByRole("button", { name: /delete support narrator/i }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/deletes the voice from the provider account/i)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /delete voice/i }));
    await waitFor(() => expect(deleteVoiceClone).toHaveBeenCalledWith("vp_clone1"));
  });
});
