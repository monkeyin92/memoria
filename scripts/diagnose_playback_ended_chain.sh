#!/bin/bash
# P0-2 诊断脚本：追踪 playback.ended 完整链路
# 使用方法：./diagnose_playback_ended_chain.sh <session_id>

set -euo pipefail

SESSION_ID="${1:-}"
if [ -z "$SESSION_ID" ]; then
    echo "Usage: $0 <session_id>"
    echo "Example: $0 a9ea0f30-5a37-4c75-baa7-65a03e19b76f"
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
OUTPUT_DIR="/tmp/memoria-playback-diagnosis-${TIMESTAMP}"
mkdir -p "$OUTPUT_DIR"

echo "========================================="
echo "Playback.ended Chain Diagnosis"
echo "Session ID: $SESSION_ID"
echo "Output: $OUTPUT_DIR"
echo "========================================="
echo

# 1. 检查 Media Edge 日志中的设备回执
echo "[1/6] Checking Media Edge device receipts..."
docker-compose logs memoria-media-edge 2>/dev/null | grep -i "$SESSION_ID" | grep -E "playback\.(started|progress|ended|error)" > "$OUTPUT_DIR/1-edge-device-receipts.log" || true

if [ -s "$OUTPUT_DIR/1-edge-device-receipts.log" ]; then
    echo "✓ Found device playback receipts in Edge"
    grep -o "playback\.[a-z]*" "$OUTPUT_DIR/1-edge-device-receipts.log" | sort | uniq -c
else
    echo "✗ NO device playback receipts found in Edge"
fi
echo

# 2. 检查 Media Edge 转发到 Voice Core 的 PlaybackProgress
echo "[2/6] Checking Edge → Voice Core gRPC messages..."
docker-compose logs memoria-media-edge 2>/dev/null | grep -i "$SESSION_ID" | grep -E "PlaybackProgress|event_type|PLAYBACK_EVENT_TYPE" > "$OUTPUT_DIR/2-edge-grpc-forwarding.log" || true

if [ -s "$OUTPUT_DIR/2-edge-grpc-forwarding.log" ]; then
    echo "✓ Found gRPC PlaybackProgress messages"
    grep -i "event_type" "$OUTPUT_DIR/2-edge-grpc-forwarding.log" | head -5
else
    echo "✗ NO gRPC PlaybackProgress messages found"
fi
echo

# 3. 检查 Voice Core Bridge 接收
echo "[3/6] Checking Voice Core Bridge reception..."
docker-compose logs memoria-voice-core-bridge 2>/dev/null | grep -i "$SESSION_ID" | grep -E "on_playback_progress|PlaybackEventType|playback.ended" > "$OUTPUT_DIR/3-bridge-reception.log" || true

if [ -s "$OUTPUT_DIR/3-bridge-reception.log" ]; then
    echo "✓ Found Voice Core Bridge playback events"
    grep -i "PlaybackEventType" "$OUTPUT_DIR/3-bridge-reception.log" | tail -5
else
    echo "✗ NO Voice Core Bridge playback events found"
fi
echo

# 4. 检查 ReplyDeliveryLedger 事件记录
echo "[4/6] Checking ReplyDeliveryLedger events..."
docker-compose logs memoria-agent 2>/dev/null | grep -i "$SESSION_ID" | grep -E "reply_delivery_event_total|playback_ended|actual_heard|PLAYBACK_ENDED" > "$OUTPUT_DIR/4-reply-delivery-events.log" || true

if [ -s "$OUTPUT_DIR/4-reply-delivery-events.log" ]; then
    echo "✓ Found ReplyDelivery events"
    grep -o "event=\"[^\"]*\"" "$OUTPUT_DIR/4-reply-delivery-events.log" | sort | uniq -c
else
    echo "✗ NO ReplyDelivery events found"
fi
echo

# 5. 检查 PlaybackLedger 状态
echo "[5/6] Checking PlaybackLedger terminal state..."
docker-compose logs memoria-agent 2>/dev/null | grep -i "$SESSION_ID" | grep -E "is_playback_complete|terminal_received|_explicit_terminal_required" > "$OUTPUT_DIR/5-playback-ledger-state.log" || true

if [ -s "$OUTPUT_DIR/5-playback-ledger-state.log" ]; then
    echo "✓ Found PlaybackLedger state logs"
    wc -l "$OUTPUT_DIR/5-playback-ledger-state.log"
else
    echo "⚠ NO PlaybackLedger debug logs (may need to enable debug logging)"
fi
echo

# 6. 检查 generation complete 信号
echo "[6/6] Checking generation completion..."
docker-compose logs memoria-agent 2>/dev/null | grep -i "$SESSION_ID" | grep -E "GENERATION_ACTION_COMPLETE|playback_completed|on_media_playback_done" > "$OUTPUT_DIR/6-generation-complete.log" || true

if [ -s "$OUTPUT_DIR/6-generation-complete.log" ]; then
    echo "✓ Found generation completion signals"
    grep -o "GENERATION_ACTION_[A-Z]*" "$OUTPUT_DIR/6-generation-complete.log" | sort | uniq -c
else
    echo "✗ NO generation completion signals found"
fi
echo

# 生成总结报告
echo "========================================="
echo "DIAGNOSIS SUMMARY"
echo "========================================="

CHAIN_STATUS=""

# 检查每个环节
if [ -s "$OUTPUT_DIR/1-edge-device-receipts.log" ]; then
    CHAIN_STATUS="${CHAIN_STATUS}✓ Device→Edge "

    if grep -q "playback\.ended" "$OUTPUT_DIR/1-edge-device-receipts.log"; then
        CHAIN_STATUS="${CHAIN_STATUS}(ENDED) "
    else
        CHAIN_STATUS="${CHAIN_STATUS}(NO ENDED) "
    fi
else
    CHAIN_STATUS="${CHAIN_STATUS}✗ Device→Edge "
fi

if [ -s "$OUTPUT_DIR/2-edge-grpc-forwarding.log" ]; then
    CHAIN_STATUS="${CHAIN_STATUS}✓ Edge→gRPC "
else
    CHAIN_STATUS="${CHAIN_STATUS}✗ Edge→gRPC "
fi

if [ -s "$OUTPUT_DIR/3-bridge-reception.log" ]; then
    CHAIN_STATUS="${CHAIN_STATUS}✓ Bridge "
else
    CHAIN_STATUS="${CHAIN_STATUS}✗ Bridge "
fi

if [ -s "$OUTPUT_DIR/4-reply-delivery-events.log" ]; then
    if grep -q "playback_ended" "$OUTPUT_DIR/4-reply-delivery-events.log"; then
        CHAIN_STATUS="${CHAIN_STATUS}✓ Ledger(ENDED) "
    else
        CHAIN_STATUS="${CHAIN_STATUS}⚠ Ledger(NO ENDED) "
    fi
else
    CHAIN_STATUS="${CHAIN_STATUS}✗ Ledger "
fi

if [ -s "$OUTPUT_DIR/6-generation-complete.log" ]; then
    CHAIN_STATUS="${CHAIN_STATUS}✓ Complete"
else
    CHAIN_STATUS="${CHAIN_STATUS"}✗ Complete"
fi

echo "$CHAIN_STATUS"
echo
echo "Detailed logs saved to: $OUTPUT_DIR"
echo

# 给出修复建议
echo "========================================="
echo "RECOMMENDATIONS"
echo "========================================="

if ! [ -s "$OUTPUT_DIR/1-edge-device-receipts.log" ]; then
    echo "❌ CRITICAL: No device receipts found"
    echo "   → Check ESP32 firmware NotifyPlaybackDrained() is called"
    echo "   → Check AudioService drain notification path"
    echo "   → Verify device WSS connection is stable"
    echo
elif ! grep -q "playback\.ended" "$OUTPUT_DIR/1-edge-device-receipts.log"; then
    echo "❌ CRITICAL: Device sends receipts but NO playback.ended"
    echo "   → Check firmware playback_terminal_receipted_ flag"
    echo "   → Check NotifyPlaybackDrained() implementation"
    echo "   → Verify AudioService completion event fires"
    echo
fi

if [ -s "$OUTPUT_DIR/1-edge-device-receipts.log" ] && ! [ -s "$OUTPUT_DIR/2-edge-grpc-forwarding.log" ]; then
    echo "❌ CRITICAL: Edge receives but doesn't forward to gRPC"
    echo "   → Check Edge device_ws_uplink.go receipt forwarding"
    echo "   → Check gRPC bridge connection status"
    echo "   → Verify PlaybackProgress message construction"
    echo
fi

if [ -s "$OUTPUT_DIR/2-edge-grpc-forwarding.log" ] && ! [ -s "$OUTPUT_DIR/3-bridge-reception.log" ]; then
    echo "❌ CRITICAL: gRPC messages sent but Bridge doesn't receive"
    echo "   → Check gRPC connection health"
    echo "   → Check Bridge on_playback_progress registration"
    echo "   → Verify media-v1 proto compatibility"
    echo
fi

if [ -s "$OUTPUT_DIR/3-bridge-reception.log" ] && ! [ -s "$OUTPUT_DIR/4-reply-delivery-events.log" ]; then
    echo "❌ CRITICAL: Bridge receives but ReplyDelivery not updated"
    echo "   → Check _record_reply_delivery_event() is called"
    echo "   → Check PlaybackLedger.acknowledge() terminal parameter"
    echo "   → Verify is_playback_complete() logic"
    echo
fi

if [ -s "$OUTPUT_DIR/4-reply-delivery-events.log" ] && ! grep -q "playback_ended" "$OUTPUT_DIR/4-reply-delivery-events.log"; then
    echo "❌ CRITICAL: Ledger events present but NO playback_ended"
    echo "   → Check _finish_completed_output() trigger conditions"
    echo "   → Verify provider_complete flag is set"
    echo "   → Check is_playback_complete() returns true"
    echo
fi

if [ -s "$OUTPUT_DIR/4-reply-delivery-events.log" ] && grep -q "playback_ended" "$OUTPUT_DIR/4-reply-delivery-events.log" && ! [ -s "$OUTPUT_DIR/6-generation-complete.log" ]; then
    echo "⚠ WARNING: playback_ended recorded but no COMPLETE action"
    echo "   → This may be acceptable if generation was cancelled"
    echo "   → Check for superseded/preempted events"
    echo
fi

if [ -s "$OUTPUT_DIR/6-generation-complete.log" ]; then
    echo "✅ SUCCESS: Full chain operational"
    echo "   → playback.ended → gRPC → Bridge → Ledger → Complete"
    echo "   → Review logs for timing and performance"
    echo
fi

echo "========================================="
echo "For next steps, see:"
echo "  docs/verification/P0-1-single-turn-verification-guide.md"
echo "========================================="
