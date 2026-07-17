import { useCallback, useEffect, useRef, useState } from "react";
import {
  Room,
  RoomEvent,
  Track,
  type Participant,
  type RemoteTrack,
  type TranscriptionSegment,
} from "livekit-client";

import type { CreateSessionResponse } from "../api/controlApi";
import type { UiState } from "../types/events";

const RECONNECT_TIMEOUT_MS = 10_000;

type VoiceRoomProps = {
  session: CreateSessionResponse;
  micEnabled: boolean;
  onData: (payload: Uint8Array) => void;
  onSynchronizedTranscript: (segments: TranscriptionSegment[]) => void;
  onConnectionState: (state: UiState, message?: string) => void;
  onReconnected: () => Promise<boolean>;
  onEnded: (error?: string) => void;
};

/** Connects only after App receives an explicit user gesture. */
export function VoiceRoom({
  session,
  micEnabled,
  onData,
  onSynchronizedTranscript,
  onConnectionState,
  onReconnected,
  onEnded,
}: VoiceRoomProps) {
  const roomRef = useRef<Room | null>(null);
  const micEnabledRef = useRef(micEnabled);
  const audioContainerRef = useRef<HTMLDivElement | null>(null);
  const audioTracksRef = useRef(new Map<string, RemoteTrack>());
  const reconnectTimerRef = useRef<number | null>(null);
  const [joined, setJoined] = useState(false);
  const [status, setStatus] = useState("未加入房间");
  const [permission, setPermission] = useState("未检查");
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState("");

  const detachAudio = useCallback((track: RemoteTrack) => {
    track.detach().forEach((element) => element.remove());
    if (track.sid) audioTracksRef.current.delete(track.sid);
  }, []);

  const detachAllAudio = useCallback(() => {
    audioTracksRef.current.forEach((track) => {
      track.detach().forEach((element) => element.remove());
    });
    audioTracksRef.current.clear();
    audioContainerRef.current?.replaceChildren();
  }, []);

  const attachAudio = useCallback((track: RemoteTrack) => {
    const container = audioContainerRef.current;
    const trackSid = track.sid;
    if (track.kind !== Track.Kind.Audio || !container || !trackSid) return;
    const previous = audioTracksRef.current.get(trackSid);
    if (previous) {
      previous.detach().forEach((element) => element.remove());
    }
    const element = track.attach();
    element.autoplay = true;
    element.setAttribute("playsinline", "");
    element.dataset.trackSid = trackSid;
    container.append(element);
    audioTracksRef.current.set(trackSid, track);
  }, []);

  const attachSubscribedAudio = useCallback(
    (room: Room) => {
      room.remoteParticipants.forEach((participant) => {
        participant.audioTrackPublications.forEach((publication) => {
          if (publication.track) attachAudio(publication.track);
        });
      });
    },
    [attachAudio],
  );

  const refreshDevices = useCallback(async (room?: Room) => {
    const available = await Room.getLocalDevices("audioinput", false);
    setDevices(available);
    setSelectedDeviceId((current) => {
      if (available.some((device) => device.deviceId === current)) return current;
      return room?.getActiveDevice("audioinput") ?? available[0]?.deviceId ?? "";
    });
    return available;
  }, []);

  useEffect(() => {
    let cancelled = false;
    const clearReconnectTimer = () => {
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };
    const room = new Room({
      adaptiveStream: true,
      dynacast: true,
      audioCaptureDefaults: {
        autoGainControl: true,
        echoCancellation: true,
        noiseSuppression: true,
        channelCount: 1,
      },
    });
    roomRef.current = room;

    const handleReconnecting = () => {
      if (cancelled) return;
      detachAllAudio();
      setStatus("连接恢复中");
      onConnectionState("reconnecting");
      clearReconnectTimer();
      reconnectTimerRef.current = window.setTimeout(() => {
        if (cancelled) return;
        const message = "连接超时，请重新连接";
        setStatus(message);
        onEnded(message);
        void room.disconnect();
      }, RECONNECT_TIMEOUT_MS);
    };

    const handleReconnected = async () => {
      if (cancelled) return;
      clearReconnectTimer();
      // Fresh elements start at the live edge instead of replaying queued audio.
      detachAllAudio();
      if (!(await onReconnected())) {
        const message = "重连同步失败，请重新连接";
        onEnded(message);
        await room.disconnect();
        return;
      }
      attachSubscribedAudio(room);
      onConnectionState("ready");
      setStatus("可以说话");
      void room.localParticipant.setMicrophoneEnabled(micEnabledRef.current);
      void room.startAudio();
      void refreshDevices(room);
    };

    const handleDisconnected = () => {
      clearReconnectTimer();
      detachAllAudio();
      if (!cancelled) onEnded("连接已断开，请重新连接");
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState !== "visible" || cancelled) return;
      void refreshDevices(room);
      void room.localParticipant.setMicrophoneEnabled(micEnabledRef.current);
      void room.startAudio();
    };

    const handleTranscription = (
      segments: TranscriptionSegment[],
      participant?: Participant,
    ) => {
      if (!cancelled && participant?.isAgent) {
        onSynchronizedTranscript(segments);
      }
    };

    room
      .on(RoomEvent.TrackSubscribed, attachAudio)
      .on(RoomEvent.TrackUnsubscribed, detachAudio)
      .on(RoomEvent.DataReceived, onData)
      .on(RoomEvent.TranscriptionReceived, handleTranscription)
      .on(RoomEvent.Reconnecting, handleReconnecting)
      .on(RoomEvent.SignalReconnecting, handleReconnecting)
      .on(RoomEvent.Reconnected, handleReconnected)
      .on(RoomEvent.Disconnected, handleDisconnected)
      .on(RoomEvent.MediaDevicesChanged, () => void refreshDevices(room))
      .on(RoomEvent.MediaDevicesError, (error) => {
        if (!cancelled) {
          setPermission("不可用");
          onConnectionState("closed", error.message);
        }
      });
    document.addEventListener("visibilitychange", handleVisibilityChange);

    void (async () => {
      try {
        setStatus("正在检查麦克风");
        const available = await Room.getLocalDevices("audioinput", true);
        if (cancelled) return;
        if (available.length === 0) throw new Error("未找到可用麦克风");
        setPermission("已授权");
        setDevices(available);
        const initialDeviceId = available[0].deviceId;
        setSelectedDeviceId(initialDeviceId);

        setStatus("正在连接");
        await room.connect(session.livekit_url, session.participant_token);
        if (cancelled) {
          await room.disconnect();
          return;
        }
        await room.switchActiveDevice("audioinput", initialDeviceId);
        await room.localParticipant.setMicrophoneEnabled(micEnabledRef.current);
        attachSubscribedAudio(room);
        await room.startAudio();
        setJoined(true);
        setStatus("可以说话");
        onConnectionState("ready");
      } catch (error) {
        if (cancelled) return;
        const denied =
          error instanceof DOMException && error.name === "NotAllowedError";
        if (denied) setPermission("已拒绝");
        const message = denied
          ? "麦克风权限被拒绝"
          : error instanceof Error
            ? error.message
            : "加入房间失败";
        setStatus(message);
        onConnectionState("closed", message);
      }
    })();

    return () => {
      cancelled = true;
      clearReconnectTimer();
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      detachAllAudio();
      void room.disconnect();
      roomRef.current = null;
    };
  }, [
    attachAudio,
    attachSubscribedAudio,
    detachAllAudio,
    detachAudio,
    onConnectionState,
    onData,
    onSynchronizedTranscript,
    onEnded,
    onReconnected,
    refreshDevices,
    session.livekit_url,
    session.participant_token,
  ]);

  useEffect(() => {
    micEnabledRef.current = micEnabled;
    const room = roomRef.current;
    if (!room || !joined) return;
    void room.localParticipant.setMicrophoneEnabled(micEnabled).catch((error: unknown) => {
      onConnectionState(
        "closed",
        error instanceof Error ? error.message : "麦克风切换失败",
      );
    });
  }, [joined, micEnabled, onConnectionState]);

  const switchMicrophone = async (deviceId: string) => {
    const room = roomRef.current;
    if (!room) return;
    try {
      await room.switchActiveDevice("audioinput", deviceId);
      setSelectedDeviceId(deviceId);
    } catch (error) {
      onConnectionState(
        "closed",
        error instanceof Error ? error.message : "麦克风切换失败",
      );
    }
  };

  return (
    <div className="voice-room">
      <p>房间：{session.room_name}</p>
      <p>状态：{status}</p>
      <p>麦克风权限：{permission}</p>
      <label>
        输入设备：
        <select
          aria-label="输入设备"
          disabled={!joined || devices.length === 0}
          value={selectedDeviceId}
          onChange={(event) => void switchMicrophone(event.target.value)}
        >
          {devices.map((device, index) => (
            <option key={device.deviceId} value={device.deviceId}>
              {device.label || `麦克风 ${index + 1}`}
            </option>
          ))}
        </select>
      </label>
      <p className="hint">助手播放时麦克风保持开启（AEC 由浏览器/WebRTC 处理）。</p>
      <div ref={audioContainerRef} hidden aria-hidden="true" />
    </div>
  );
}
