"""
修复 Listening State 卡住问题 - 代码补丁

问题：ListeningStateManager 已创建但未与 VAD 事件连接
解决：在 VAD 事件处理中添加状态转换调用

使用方法：
1. 应用 Patch 1 到 grpc_bridge.py
2. 应用 Patch 2 到 media_session_lifecycle.py（如果需要访问 _audio_ingress）
3. 应用 Patch 3 到 media_session_input.py（完整集成）
"""

# ============================================================================
# Patch 1: grpc_bridge.py - VAD 事件处理
# ============================================================================
# 文件: services/agent/src/voice_core/grpc_bridge.py
# 位置: 第 794-796 行之后

PATCH_1_LOCATION = """
            if accepted and self.on_speech_segment is not None:
                await self.on_speech_segment(connection.session, segment, 0)
            # 🔽 在这里添加新代码
            return
"""

PATCH_1_NEW_CODE = """
            if accepted and self.on_speech_segment is not None:
                await self.on_speech_segment(connection.session, segment, 0)

            # 🆕 Integrate with ListeningStateManager for state transitions
            # This connects VAD events to the listening state lifecycle
            if accepted:
                try:
                    # Access the audio ingress from session state
                    ingress_state = connection.session.ingress
                    session_id = connection.session.identity.session_id

                    # Import locally to avoid circular dependency
                    from services.agent.src.voice_core.media_audio_ingress import MediaAudioIngress

                    # Get the MediaAudioIngress instance from the session owner
                    # Note: This assumes the session owner has _audio_ingress attribute
                    # If connection has owner reference, use it; otherwise log warning
                    if hasattr(connection.session, '_lifecycle_owner'):
                        owner = connection.session._lifecycle_owner
                        if hasattr(owner, '_audio_ingress'):
                            audio_ingress = owner._audio_ingress

                            # Trigger state transition based on VAD event type
                            if speech_end:
                                await audio_ingress._state_manager.on_speech_ended(session_id)
                                logger.debug(
                                    "VAD speech_end: triggered listening state transition session=%s",
                                    session_id
                                )
                            else:
                                await audio_ingress._state_manager.on_speech_detected(session_id)
                                logger.debug(
                                    "VAD speech_start: updated listening state session=%s",
                                    session_id
                                )
                except Exception as e:
                    # Don't break VAD processing if state management fails
                    logger.warning(
                        "Failed to update listening state from VAD event session=%s: %s",
                        connection.session.identity.session_id,
                        e,
                        exc_info=True
                    )

            return
"""

# ============================================================================
# Patch 2: media_session_lifecycle.py - 在 on_speech_segment 中集成
# ============================================================================
# 文件: services/agent/src/voice_core/media_session_lifecycle.py
# 位置: on_speech_segment 方法中

PATCH_2_DESCRIPTION = """
在 on_speech_segment 方法中添加状态转换调用。
这是更清晰的方案，因为 lifecycle 已经有 self._audio_ingress 引用。

找到 on_speech_segment 方法，在处理完 segment 后添加：
"""

PATCH_2_NEW_CODE = """
    async def on_speech_segment(
        self,
        session: MediaBridgeSession,
        segment: SpeechSegment,
        stream_epoch: int = 0,
    ) -> None:
        # ... 现有代码 ...

        # 🆕 Integrate with ListeningStateManager
        # Trigger state transitions based on VAD events
        if segment.kind == SegmentKind.VAD:
            try:
                session_id = segment.session_id
                if segment.final:
                    # VAD speech_end event
                    await self._audio_ingress._state_manager.on_speech_ended(session_id)
                    logger.debug(
                        "VAD speech_end: triggered listening state transition session=%s",
                        session_id
                    )
                else:
                    # VAD speech_start event
                    await self._audio_ingress._state_manager.on_speech_detected(session_id)
                    logger.debug(
                        "VAD speech_start: updated listening state session=%s",
                        session_id
                    )
            except Exception as e:
                # Don't break VAD processing if state management fails
                logger.warning(
                    "Failed to update listening state from VAD segment session=%s: %s",
                    session_id,
                    e,
                    exc_info=True
                )
"""

# ============================================================================
# Patch 3: media_session_input.py - 在 finalize_speech_segment 中集成
# ============================================================================
# 文件: services/agent/src/voice_core/media_session_input.py
# 位置: finalize_speech_segment 方法的末尾

PATCH_3_DESCRIPTION = """
在 ASR 终止时转换到 PROCESSING 状态。
这确保即使 VAD 集成失败，ASR 完成时也会正确转换状态。

在 finalize_speech_segment 方法的 return True 之前添加：
"""

PATCH_3_NEW_CODE = """
            # ... 现有的 finalize 逻辑 ...

            # 🆕 Transition to PROCESSING state after speech finalization
            # This ensures we stop accepting new audio after the turn is committed
            if finalize_reason in ("vad_end", "turn_commit", "explicit"):
                try:
                    session_id = context.identity.session_id
                    # Access audio ingress from the session owner (self is the lifecycle mixin)
                    if hasattr(self, '_audio_ingress'):
                        await self._audio_ingress._state_manager.transition_to_processing(session_id)
                        logger.debug(
                            "ASR finalized: transitioned to PROCESSING state session=%s reason=%s",
                            session_id,
                            finalize_reason
                        )
                except Exception as e:
                    # Don't break finalization if state management fails
                    logger.warning(
                        "Failed to transition to PROCESSING state session=%s: %s",
                        context.identity.session_id,
                        e,
                        exc_info=True
                    )

            return True
"""

# ============================================================================
# Patch 4: 在回复生成开始时转换到 SPEAKING 状态
# ============================================================================

PATCH_4_DESCRIPTION = """
当系统开始生成回复时，转换到 SPEAKING 状态。
这样可以根据配置允许或禁止打断。

找到开始生成回复的位置（可能在 media_session_output.py 或类似文件），添加：
"""

PATCH_4_NEW_CODE = """
    async def start_reply_generation(self, context: _MediaVoiceSession):
        # ... 现有代码 ...

        # 🆕 Transition to SPEAKING state
        try:
            session_id = context.identity.session_id
            if hasattr(self, '_audio_ingress'):
                await self._audio_ingress._state_manager.transition_to_speaking(session_id)
                logger.debug(
                    "Reply generation started: transitioned to SPEAKING state session=%s",
                    session_id
                )
        except Exception as e:
            logger.warning(
                "Failed to transition to SPEAKING state session=%s: %s",
                session_id,
                e,
                exc_info=True
            )
"""

# ============================================================================
# 推荐实施顺序
# ============================================================================

IMPLEMENTATION_ORDER = """
推荐实施顺序：

1. **立即修复** (5分钟)
   - 应用 Patch 2 到 media_session_lifecycle.py 的 on_speech_segment 方法
   - 这是最干净的方案，因为 lifecycle 已经有 _audio_ingress 引用

2. **完整集成** (10分钟)
   - 应用 Patch 3 到 media_session_input.py 的 finalize_speech_segment 方法
   - 确保 ASR 完成时也会转换状态

3. **可选增强** (10分钟)
   - 应用 Patch 4 到回复生成的入口点
   - 实现完整的状态机：IDLE → LISTENING → PROCESSING → SPEAKING → IDLE

测试步骤：
1. 重启 agent 服务
2. 按下按钮，观察日志：
   - 应该看到 "VAD speech_start: updated listening state"
   - 应该看到 "VAD speech_end: triggered listening state transition"
   - 应该看到 "ASR finalized: transitioned to PROCESSING state"
3. 确认单轮对话不再卡在"聆听中"
"""

if __name__ == "__main__":
    print(__doc__)
    print("\n" + "="*80)
    print("PATCH 1 - grpc_bridge.py")
    print("="*80)
    print(PATCH_1_NEW_CODE)

    print("\n" + "="*80)
    print("PATCH 2 - media_session_lifecycle.py (推荐)")
    print("="*80)
    print(PATCH_2_DESCRIPTION)
    print(PATCH_2_NEW_CODE)

    print("\n" + "="*80)
    print("PATCH 3 - media_session_input.py")
    print("="*80)
    print(PATCH_3_DESCRIPTION)
    print(PATCH_3_NEW_CODE)

    print("\n" + "="*80)
    print("PATCH 4 - 回复生成 (可选)")
    print("="*80)
    print(PATCH_4_DESCRIPTION)
    print(PATCH_4_NEW_CODE)

    print("\n" + "="*80)
    print(IMPLEMENTATION_ORDER)
    print("="*80)
