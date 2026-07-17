import { useState } from "react";

import { ConnectionBanner } from "./components/ConnectionBanner";
import { TranscriptPanel } from "./components/TranscriptPanel";
import { VoiceRoom } from "./components/VoiceRoom";
import { useVoiceSession } from "./hooks/useVoiceSession";

export default function App() {
  const voice = useVoiceSession();
  const [audioUnlocked, setAudioUnlocked] = useState(false);

  return (
    <main className="app">
      <h1>中文全双工语音助手</h1>
      <ConnectionBanner state={voice.uiState} error={voice.error} />

      <div className="controls">
        <button
          type="button"
          disabled={voice.connecting || !!voice.session}
          onClick={async () => {
            // Explicit gesture unlocks autoplay.
            setAudioUnlocked(true);
            await voice.start();
          }}
        >
          开始会话
        </button>
        <button
          type="button"
          disabled={!voice.session}
          onClick={() => void voice.stopAssistant()}
        >
          停止回答
        </button>
        <button
          type="button"
          disabled={!voice.session}
          onClick={() => voice.setMicEnabled(!voice.micEnabled)}
        >
          {voice.micEnabled ? "关闭麦克风" : "打开麦克风"}
        </button>
        <button
          type="button"
          disabled={!voice.session}
          onClick={() => voice.endSession()}
        >
          结束会话
        </button>
      </div>

      {voice.session && audioUnlocked ? (
        <VoiceRoom
          session={voice.session}
          micEnabled={voice.micEnabled}
          onData={voice.handleData}
          onSynchronizedTranscript={voice.handleSynchronizedTranscript}
          onConnectionState={voice.handleConnectionState}
          onReconnected={voice.handleReconnected}
          onEnded={voice.endSession}
        />
      ) : (
        <p className="hint">点击「开始会话」以请求麦克风并连接 LiveKit。</p>
      )}

      <TranscriptPanel lines={voice.transcripts} />
    </main>
  );
}
