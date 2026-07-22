import { useEffect, useRef, useState } from "react";
import {
  CaretLeft,
  CaretRight,
  Check,
  Microphone,
  Pause,
  Play,
  SpeakerHigh,
  Stop,
} from "@phosphor-icons/react";

import { enrollSpeakerProfiles, updateProfile } from "../api.js";
import {
  companionById,
  companions,
  defaultCompanionId,
} from "../lib/companions.js";
import { createSpeakerPcmRecorder } from "../lib/audioEnrollment.js";
import { MascotVisual } from "./Mascot.jsx";

const expressions = [
  { id: "neutral", label: "平静" },
  { id: "happy", label: "开心" },
  { id: "curious", label: "好奇" },
  { id: "caring", label: "关切" },
];

const enrollmentPrompts = [
  "我是你的主人，今天想让你认识我的声音。",
  "无论开心还是疲惫，我都会用这样的声音和你说话。",
  "请记住我的声音，也尊重只有我能决定如何使用它。",
];

function enrollmentError(error, fallback) {
  if (["NotAllowedError", "SecurityError"].includes(error?.name)) {
    return "需要麦克风权限才能建立声纹，请在浏览器设置中允许后重试。";
  }
  if (["NotFoundError", "DevicesNotFoundError"].includes(error?.name)) {
    return "没有找到可用的麦克风，请连接麦克风后重试。";
  }
  return error?.message || fallback;
}

export function CompanionOnboarding({ userId, onComplete }) {
  const [stage, setStage] = useState("choose");
  const [selectedId, setSelectedId] = useState(defaultCompanionId);
  const [expression, setExpression] = useState("neutral");
  const [playing, setPlaying] = useState(false);
  const [consent, setConsent] = useState(false);
  const [recordings, setRecordings] = useState([null, null, null]);
  const [recordingIndex, setRecordingIndex] = useState(null);
  const [recordingSeconds, setRecordingSeconds] = useState(0);
  const [busy, setBusy] = useState(false);
  const [enrollmentCreated, setEnrollmentCreated] = useState(false);
  const [error, setError] = useState("");
  const carouselRef = useRef(null);
  const audioRef = useRef(null);
  const streamRef = useRef(null);
  const recorderRef = useRef(null);
  const screenRef = useRef(null);
  const recordingTimerRef = useRef(null);
  const recordingStartedAtRef = useRef(0);
  const selected = companionById(selectedId);
  const selectedIndex = companions.findIndex(({ id }) => id === selectedId);

  const pausePreview = () => {
    audioRef.current?.pause();
    setPlaying(false);
  };

  const stopRecording = () => {
    window.clearInterval(recordingTimerRef.current);
    recordingTimerRef.current = null;
    const recorder = recorderRef.current;
    recorderRef.current = null;
    if (!recorder) return;
    void recorder.stop().then((sample) => {
      if (!sample) return;
      setRecordings((current) => current.map((item, itemIndex) => (
        itemIndex === recordingIndex ? sample : item
      )));
      setRecordingIndex(null);
      setRecordingSeconds(0);
    }).catch((recordingError) => {
      setRecordingIndex(null);
      setRecordingSeconds(0);
      setError(enrollmentError(recordingError, "录音没有保存，请重试。"));
    });
  };

  const releaseMicrophone = () => {
    window.clearInterval(recordingTimerRef.current);
    recordingTimerRef.current = null;
    void recorderRef.current?.cancel?.();
    recorderRef.current = null;
    setRecordingIndex(null);
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  };

  useEffect(
    () => () => {
      audioRef.current?.pause();
      window.clearInterval(recordingTimerRef.current);
      void recorderRef.current?.cancel?.();
      streamRef.current?.getTracks().forEach((track) => track.stop());
    },
    [],
  );

  useEffect(() => {
    if (screenRef.current) screenRef.current.scrollTop = 0;
    if (stage === "choose") {
      carouselRef.current?.children[selectedIndex]?.scrollIntoView?.({
        behavior: "auto",
        inline: "center",
        block: "nearest",
      });
    }
  }, [stage]);

  const selectCompanion = (index, scroll = true) => {
    const companion = companions[index];
    if (!companion) return;
    pausePreview();
    setSelectedId(companion.id);
    setExpression("neutral");
    setError("");
    if (scroll) {
      carouselRef.current?.children[index]?.scrollIntoView?.({
        behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
          ? "auto"
          : "smooth",
        inline: "center",
        block: "nearest",
      });
    }
  };

  const syncCarouselSelection = () => {
    const carousel = carouselRef.current;
    if (!carousel) return;
    const center = carousel.scrollLeft + carousel.clientWidth / 2;
    const closest = Array.from(carousel.children).reduce(
      (best, card, index) => {
        const distance = Math.abs(card.offsetLeft + card.offsetWidth / 2 - center);
        return distance < best.distance ? { index, distance } : best;
      },
      { index: selectedIndex, distance: Number.POSITIVE_INFINITY },
    );
    if (closest.index !== selectedIndex) selectCompanion(closest.index, false);
  };

  const togglePreview = async () => {
    const audio = audioRef.current;
    if (!audio) return;
    setError("");
    if (!audio.paused) {
      pausePreview();
      return;
    }
    try {
      audio.currentTime = 0;
      await audio.play();
      setPlaying(true);
    } catch {
      setError("声音试听没有播放，请检查静音设置后重试。");
    }
  };

  const startRecording = async (index) => {
    if (!consent) {
      setError("请先确认声纹授权。");
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setError("当前浏览器不支持现场录音，请换用较新的 Safari 或 Chrome。");
      return;
    }
    setError("");
    try {
      if (!streamRef.current) {
        streamRef.current = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
          video: false,
        });
      }
      const recorder = createSpeakerPcmRecorder(streamRef.current);
      recorderRef.current = recorder;
      await recorder.start();
      recordingStartedAtRef.current = Date.now();
      setRecordingIndex(index);
      setRecordingSeconds(0);
      recordingTimerRef.current = window.setInterval(() => {
        const elapsed = (Date.now() - recordingStartedAtRef.current) / 1000;
        setRecordingSeconds(elapsed);
        if (elapsed >= 12) stopRecording();
      }, 100);
    } catch (recordingError) {
      void recorderRef.current?.cancel?.();
      recorderRef.current = null;
      setRecordingIndex(null);
      setError(enrollmentError(recordingError, "录音没有开始，请稍后重试。"));
    }
  };

  const finishOnboarding = async () => {
    if (recordings.some((recording) => !recording)) {
      setError("请先完成三段录音。");
      return;
    }
    setBusy(true);
    setError("");
    let voiceprintReady = enrollmentCreated;
    try {
      if (!enrollmentCreated) {
        await enrollSpeakerProfiles(recordings);
        voiceprintReady = true;
        setEnrollmentCreated(true);
      }
      const savedProfile = await updateProfile(userId, { companion_id: selected.id });
      releaseMicrophone();
      onComplete({ ...savedProfile, companion_id: selected.id });
    } catch (submitError) {
      setError(enrollmentError(
        submitError,
        voiceprintReady
          ? "声纹已建立，但角色设置尚未保存，请再次完成设置。"
          : "声纹登记没有完成，请检查三段录音后重试。",
      ));
    } finally {
      setBusy(false);
    }
  };

  if (stage === "enroll") {
    return (
      <section
        className="screen companion-onboarding enrollment-step"
        aria-label="录制声纹"
        ref={screenRef}
      >
        <header className="onboarding-header">
          <button
            type="button"
            className="onboarding-back"
            onClick={() => {
              releaseMicrophone();
              setStage("choose");
              setError("");
            }}
          >
            <CaretLeft size={18} weight="bold" /> 重新选择
          </button>
          <span>2 / 2</span>
        </header>

        <div className="enrollment-companion">
          <MascotVisual
            companionId={selected.id}
            emotion="happy"
            className="enrollment-companion-visual"
          />
          <div><span>你的培育伙伴</span><strong>{selected.name}</strong></div>
        </div>

        <div className="onboarding-copy">
          <p className="eyebrow">让它先认识你</p>
          <h1>录下三段自然说话</h1>
          <p>用平常聊天的音量参考每句话，每段保持约 3–8 秒。</p>
        </div>

        <label className="voiceprint-consent">
          <input
            type="checkbox"
            checked={consent}
            onChange={(event) => setConsent(event.target.checked)}
          />
          <span>我同意生成可随时撤销的声纹模板</span>
        </label>

        <div className="recording-list">
          {enrollmentPrompts.map((prompt, index) => {
            const isRecording = recordingIndex === index;
            const isReady = Boolean(recordings[index]);
            return (
              <article className="recording-item" key={prompt} data-ready={isReady}>
                <span className="recording-number">
                  {isReady ? <Check size={16} weight="bold" /> : index + 1}
                </span>
                <p>{prompt}</p>
                <button
                  type="button"
                  className={isRecording ? "recording-stop" : "recording-start"}
                  aria-label={isRecording ? `停止第 ${index + 1} 段录音` : `${isReady ? "重录" : "录制"}第 ${index + 1} 段`}
                  title={isRecording ? "停止录音" : isReady ? "重新录制" : "开始录音"}
                  disabled={recordingIndex !== null && !isRecording || busy}
                  onClick={() => isRecording ? stopRecording() : void startRecording(index)}
                >
                  {isRecording ? <Stop size={19} weight="fill" /> : <Microphone size={19} weight="fill" />}
                </button>
                {isRecording && (
                  <span className="recording-time">{recordingSeconds.toFixed(1)}s</span>
                )}
              </article>
            );
          })}
        </div>

        <p className="voiceprint-note">
          系统会检查时长、清晰度和声学质量，不会用 ASR 强制逐字比对；三段录音只用于生成声纹模板。档案会先进入影子评估，不会自动启用主人判定，也不是声音克隆。
        </p>
        {error && <p className="onboarding-error" role="alert">{error}</p>}
        <button
          type="button"
          className="onboarding-primary"
          disabled={!consent || recordings.some((recording) => !recording) || recordingIndex !== null || busy}
          onClick={() => void finishOnboarding()}
        >
          {busy ? "正在建立声纹…" : enrollmentCreated ? "完成设置" : "建立声纹并开始陪伴"}
        </button>
      </section>
    );
  }

  return (
    <section
      className="screen companion-onboarding choose-step"
      aria-label="选择陪伴方式"
      ref={screenRef}
    >
      <header className="onboarding-header">
        <span className="onboarding-brand">Memoria</span>
        <span>1 / 2</span>
      </header>
      <div className="onboarding-copy">
        <p className="eyebrow">先选它怎样陪你</p>
        <h1>选择喜欢的陪伴方式</h1>
        <p>这里只调整语气、回复长短和提问深浅，以后可以随时更换。</p>
      </div>

      <div className="companion-boundaries" aria-label="陪伴方式与数字分身的边界">
        <p><strong>陪伴方式</strong>决定助手怎样回应，不代表你的性格。</p>
        <p><strong>数字分身</strong>只从你本人说过、确认或纠正的内容成长；伙伴说的话不会成为你的证据。</p>
      </div>

      <div className="companion-carousel-shell">
        <button
          type="button"
          className="carousel-arrow carousel-previous"
          aria-label="上一个伙伴"
          title="上一个"
          disabled={selectedIndex === 0}
          onClick={() => selectCompanion(selectedIndex - 1)}
        >
          <CaretLeft size={21} weight="bold" />
        </button>
        <div
          className="companion-carousel"
          ref={carouselRef}
          tabIndex="0"
          onScroll={syncCarouselSelection}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft") selectCompanion(selectedIndex - 1);
            if (event.key === "ArrowRight") selectCompanion(selectedIndex + 1);
          }}
        >
          {companions.map((companion, index) => (
            <button
              type="button"
              className="companion-card"
              data-selected={companion.id === selected.id}
              aria-pressed={companion.id === selected.id}
              aria-label={`选择${companion.name}，${companion.tagline.replaceAll(" · ", "、")}`}
              key={companion.id}
              onClick={() => selectCompanion(index)}
            >
              <MascotVisual
                companionId={companion.id}
                emotion={companion.id === selected.id ? expression : "neutral"}
                className="companion-card-visual"
              />
              <span><strong>{companion.name}</strong><small>{companion.tagline}</small></span>
            </button>
          ))}
        </div>
        <button
          type="button"
          className="carousel-arrow carousel-next"
          aria-label="下一个伙伴"
          title="下一个"
          disabled={selectedIndex === companions.length - 1}
          onClick={() => selectCompanion(selectedIndex + 1)}
        >
          <CaretRight size={21} weight="bold" />
        </button>
      </div>

      <div className="companion-pagination" aria-label="陪伴方式分页">
        {companions.map((companion, index) => (
          <button
            type="button"
            key={companion.id}
            className={companion.id === selected.id ? "active" : ""}
            aria-label={`查看${companion.name}`}
            aria-current={companion.id === selected.id ? "true" : undefined}
            onClick={() => selectCompanion(index)}
          />
        ))}
      </div>

      <div className="companion-detail" aria-live="polite">
        <div className="companion-detail-heading">
          <div><h2>{selected.name}</h2><p>{selected.tagline}</p></div>
          <span>{selectedIndex + 1} / {companions.length}</span>
        </div>
        <p className="companion-description">{selected.description}</p>

        <div className="expression-segments" role="group" aria-label="演示表情">
          {expressions.map((item) => (
            <button
              type="button"
              key={item.id}
              className={expression === item.id ? "active" : ""}
              aria-pressed={expression === item.id}
              onClick={() => setExpression(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>

        <div className="voice-preview-row">
          <span className="voice-preview-icon"><SpeakerHigh size={20} weight="fill" /></span>
          <span><strong>{selected.voiceName}</strong><small>{selected.voiceDescription}</small></span>
          <button
            type="button"
            aria-label={playing ? `暂停${selected.name}的声音` : `试听${selected.name}的声音`}
            title={playing ? "暂停试听" : "试听声音"}
            onClick={() => void togglePreview()}
          >
            {playing ? <Pause size={19} weight="fill" /> : <Play size={19} weight="fill" />}
          </button>
          <audio
            key={selected.id}
            ref={audioRef}
            src={selected.voicePreview}
            preload="metadata"
            onEnded={() => setPlaying(false)}
          />
        </div>
      </div>

      {error && <p className="onboarding-error" role="alert">{error}</p>}
      <button
        type="button"
        className="onboarding-primary"
        onClick={() => {
          pausePreview();
          setStage("enroll");
          setError("");
        }}
      >
        让 {selected.name} 陪我
        <CaretRight size={19} weight="bold" />
      </button>
    </section>
  );
}
