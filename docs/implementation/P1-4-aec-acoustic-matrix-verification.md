# P1-4 AEC 声学矩阵验收

## 目标

对 ESP32 全双工声学系统进行完整验收，确保 AEC（回声消除）在各种真实场景下都能正常工作，为生产环境开放全双工对话做好准备。

## 背景

根据整改方案第 9.5 节，AEC 声学矩阵验收必须覆盖：
- 不同场景（Far-end only、Near-end only、Double-talk）
- 不同音量（20%、50%、80%、100%）
- 不同距离（0.3m、1m、2m、3m）
- 不同方位（正面、侧面、背面）
- 不同环境（安静、电视、音乐、风扇、餐厅噪声）
- 不同声音（成年男/女、儿童、老人、轻声、快速语速）
- 不同内容（普通抢话、停止词、短附和、连续长句）

只有通过这个矩阵，才能标记为 `full_duplex_verified`。

## 验收矩阵

### 核心测试场景

#### 场景 A：Far-end Only（仅播放）

**目的**：验证 AEC 能够消除回声，不会误触发 VAD

| 测试项 | 条件 | 验收标准 |
|---|---|---|
| A1 | 音量 20%，安静环境 | VAD 误触发率 < 2% |
| A2 | 音量 50%，安静环境 | VAD 误触发率 < 5% |
| A3 | 音量 80%，安静环境 | VAD 误触发率 < 10% |
| A4 | 音量 100%，安静环境 | VAD 误触发率 < 15% |
| A5 | 音量 50%，背景音乐 | VAD 误触发率 < 10% |
| A6 | 音量 50%，电视声音 | VAD 误触发率 < 15% |

**测试方法**：
1. 让设备播放 5 分钟连续回复
2. 房间内无人说话
3. 记录 VAD 触发次数和持续时间
4. 计算误触发率 = 触发时长 / 总播放时长

#### 场景 B：Near-end Only（仅采集）

**目的**：验证不播放时 ASR 识别率正常，AEC 不影响近端信号

| 测试项 | 条件 | 验收标准 |
|---|---|---|
| B1 | 成年男性，1m，安静 | ASR 字错率 < 5% |
| B2 | 成年女性，1m，安静 | ASR 字错率 < 5% |
| B3 | 儿童，1m，安静 | ASR 字错率 < 10% |
| B4 | 老人，1m，安静 | ASR 字错率 < 10% |
| B5 | 成年男性，0.5m，安静 | ASR 字错率 < 3% |
| B6 | 成年男性，2m，安静 | ASR 字错率 < 8% |
| B7 | 成年男性，3m，安静 | ASR 字错率 < 15% |
| B8 | 成年男性，1m，背景音乐 | ASR 字错率 < 10% |

**测试方法**：
1. 准备标准测试语料（100 句）
2. 不同说话人按测试条件朗读
3. 对比 ASR 识别结果与标准答案
4. 计算字错率 WER

#### 场景 C：Double-talk（双讲）

**目的**：验证播放时用户说话能被正确采集和识别（全双工核心）

| 测试项 | 条件 | 验收标准 |
|---|---|---|
| C1 | 音量 20%，1m，正面 | ASR 字错率 < 15% |
| C2 | 音量 50%，1m，正面 | ASR 字错率 < 20% |
| C3 | 音量 80%，1m，正面 | ASR 字错率 < 30% |
| C4 | 音量 50%，0.5m，正面 | ASR 字错率 < 15% |
| C5 | 音量 50%，2m，正面 | ASR 字错率 < 25% |
| C6 | 音量 50%，1m，侧面 | ASR 字错率 < 25% |
| C7 | 音量 50%，1m，背面 | ASR 字错率 < 35% |
| C8 | 音量 50%，1m，儿童 | ASR 字错率 < 30% |

**测试方法**：
1. 设备播放连续回复
2. 测试者在指定条件下说测试语料
3. 对比 ASR 识别结果
4. 计算字错率

### 声学质量指标

#### ERLE（Echo Return Loss Enhancement）

**定义**：回声消除后的残余回声能量降低程度

**测量方法**：
```python
# 播放已知信号 x(t)
# 采集麦克风信号 y(t)（包含回声）
# AEC 输出 e(t)（回声消除后）

ERLE_dB = 10 * log10(energy(y) / energy(e))
```

**验收标准**：
- 安静环境：ERLE > 20 dB
- 一般环境：ERLE > 15 dB
- 嘈杂环境：ERLE > 10 dB

#### SNR（Signal-to-Noise Ratio）

**定义**：双讲时近端信号与残余回声的比值

**验收标准**：
- Double-talk SNR > 10 dB

### 实时性能指标

| 指标 | 目标 |
|---|---|
| AEC 处理延迟 | < 30 ms |
| VAD 触发延迟 | < 100 ms |
| CPU 占用（AFE + AEC） | < 30% |
| 内存占用（堆 + PSRAM） | < 2 MB |
| 音频欠载率 | < 0.1% |
| 看门狗触发 | 0 |

## 测试工具

### 1. 自动化测试脚本

**新增文件**：`scripts/acoustic_matrix_test.py`

```python
"""Acoustic matrix verification test suite."""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import List
import serial
import sounddevice as sd
import numpy as np
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TestCase:
    """Single acoustic test case."""
    
    test_id: str
    category: str  # far_end_only | near_end_only | double_talk
    volume: int  # 0-100
    distance_m: float
    direction: str  # front | side | back
    environment: str  # quiet | music | tv | fan
    speaker_type: str  # adult_male | adult_female | child | elder
    test_corpus: List[str]
    expected_wer: float  # Maximum acceptable WER


@dataclass
class TestResult:
    """Result of a test case."""
    
    test_id: str
    passed: bool
    actual_wer: float
    vad_false_trigger_rate: float
    erle_db: float
    cpu_usage: float
    notes: str


class AcousticMatrixTester:
    """Automated acoustic matrix test runner."""
    
    def __init__(self, device_port: str, session_id: str):
        self.device_port = device_port
        self.session_id = session_id
        self.serial = None
        self.results: List[TestResult] = []
    
    async def run_matrix(self, test_cases: List[TestCase]):
        """Run complete test matrix."""
        
        logger.info("Starting acoustic matrix test with %d cases", len(test_cases))
        
        # Connect to device
        self.serial = serial.Serial(self.device_port, 115200, timeout=1)
        
        for i, test_case in enumerate(test_cases, 1):
            logger.info("Running test %d/%d: %s", i, len(test_cases), test_case.test_id)
            
            result = await self.run_test_case(test_case)
            self.results.append(result)
            
            # Log result
            status = "✅ PASS" if result.passed else "❌ FAIL"
            logger.info("%s %s: WER=%.1f%% (expected ≤%.1f%%)",
                       status, test_case.test_id,
                       result.actual_wer * 100,
                       test_case.expected_wer * 100)
            
            # Cool down
            await asyncio.sleep(2)
        
        # Close connection
        self.serial.close()
        
        # Generate report
        self.generate_report()
    
    async def run_test_case(self, test_case: TestCase) -> TestResult:
        """Run single test case."""
        
        # 1. Set device volume
        await self.set_device_volume(test_case.volume)
        
        # 2. Run test based on category
        if test_case.category == "far_end_only":
            return await self.run_far_end_only_test(test_case)
        elif test_case.category == "near_end_only":
            return await self.run_near_end_only_test(test_case)
        elif test_case.category == "double_talk":
            return await self.run_double_talk_test(test_case)
        else:
            raise ValueError(f"Unknown category: {test_case.category}")
    
    async def run_far_end_only_test(self, test_case: TestCase) -> TestResult:
        """Test far-end only (playback without user speech)."""
        
        # Trigger device playback
        await self.trigger_long_playback()
        
        # Monitor VAD triggers
        vad_triggers = 0
        total_time = 0
        
        start_time = asyncio.get_event_loop().time()
        
        while (asyncio.get_event_loop().time() - start_time) < 300:  # 5 min
            line = self.serial.readline().decode('utf-8', errors='ignore')
            
            if 'VAD triggered' in line:
                vad_triggers += 1
            
            await asyncio.sleep(0.1)
        
        total_time = asyncio.get_event_loop().time() - start_time
        vad_false_trigger_rate = vad_triggers / total_time if total_time > 0 else 0
        
        passed = vad_false_trigger_rate <= (test_case.expected_wer or 0.05)
        
        return TestResult(
            test_id=test_case.test_id,
            passed=passed,
            actual_wer=0.0,
            vad_false_trigger_rate=vad_false_trigger_rate,
            erle_db=0.0,  # TODO: measure from logs
            cpu_usage=0.0,  # TODO: measure from logs
            notes="",
        )
    
    async def run_near_end_only_test(self, test_case: TestCase) -> TestResult:
        """Test near-end only (user speech without playback)."""
        
        # Play test corpus and collect ASR results
        asr_results = []
        
        for i, ground_truth in enumerate(test_case.test_corpus):
            # Instruct user to speak
            logger.info("Please say: %s", ground_truth)
            input("Press Enter when ready...")
            
            # Capture ASR result
            asr_text = await self.capture_asr_result(timeout=10)
            asr_results.append((ground_truth, asr_text))
        
        # Calculate WER
        wer = self.calculate_wer(asr_results)
        passed = wer <= test_case.expected_wer
        
        return TestResult(
            test_id=test_case.test_id,
            passed=passed,
            actual_wer=wer,
            vad_false_trigger_rate=0.0,
            erle_db=0.0,
            cpu_usage=0.0,
            notes="",
        )
    
    async def run_double_talk_test(self, test_case: TestCase) -> TestResult:
        """Test double-talk (playback + user speech simultaneously)."""
        
        # Start device playback
        await self.trigger_long_playback()
        
        # Wait for playback to start
        await asyncio.sleep(2)
        
        # Play test corpus during playback
        asr_results = []
        
        for ground_truth in test_case.test_corpus:
            logger.info("While device is speaking, say: %s", ground_truth)
            input("Press Enter when ready...")
            
            asr_text = await self.capture_asr_result(timeout=10)
            asr_results.append((ground_truth, asr_text))
            
            await asyncio.sleep(1)
        
        # Calculate WER
        wer = self.calculate_wer(asr_results)
        passed = wer <= test_case.expected_wer
        
        return TestResult(
            test_id=test_case.test_id,
            passed=passed,
            actual_wer=wer,
            vad_false_trigger_rate=0.0,
            erle_db=0.0,
            cpu_usage=0.0,
            notes="",
        )
    
    def calculate_wer(self, results: List[tuple]) -> float:
        """Calculate Word Error Rate."""
        # TODO: Implement actual WER calculation
        # Using Levenshtein distance at word level
        return 0.0
    
    def generate_report(self):
        """Generate test report."""
        
        output_path = Path(f"acoustic_test_report_{self.session_id}.md")
        
        with output_path.open('w') as f:
            f.write("# Acoustic Matrix Test Report\n\n")
            f.write(f"Session ID: {self.session_id}\n\n")
            
            passed_count = sum(1 for r in self.results if r.passed)
            total_count = len(self.results)
            
            f.write(f"## Summary\n\n")
            f.write(f"- Total: {total_count}\n")
            f.write(f"- Passed: {passed_count}\n")
            f.write(f"- Failed: {total_count - passed_count}\n")
            f.write(f"- Pass Rate: {passed_count / total_count * 100:.1f}%\n\n")
            
            f.write("## Detailed Results\n\n")
            f.write("| Test ID | Status | WER | VAD False Trigger | ERLE | Notes |\n")
            f.write("|---------|--------|-----|-------------------|------|-------|\n")
            
            for result in self.results:
                status = "✅" if result.passed else "❌"
                f.write(f"| {result.test_id} | {status} | {result.actual_wer*100:.1f}% | "
                       f"{result.vad_false_trigger_rate*100:.1f}% | {result.erle_db:.1f} dB | "
                       f"{result.notes} |\n")
        
        logger.info("Report saved to %s", output_path)
```

### 2. 测试语料库

**创建文件**：`test_data/acoustic_test_corpus.json`

```json
{
  "near_end_basic": [
    "今天天气很好",
    "我想问一个问题",
    "你能帮我查一下吗",
    "谢谢你的帮助",
    "明天见"
  ],
  "double_talk_questions": [
    "等一下",
    "停一下",
    "那什么是机器学习",
    "你能再说一遍吗",
    "我有个问题"
  ],
  "backchannel": [
    "嗯",
    "对",
    "好的",
    "是的",
    "明白了"
  ]
}
```

## 执行流程

### 阶段 1：基础功能验证（1-2 天）

1. **单场景测试**
   - Far-end only @ 50%
   - Near-end only @ 1m
   - Double-talk @ 50%, 1m

2. **快速检查**
   - 是否有严重问题（完全无 AEC、崩溃等）
   - CPU/内存是否可接受

### 阶段 2：核心矩阵验证（3-5 天）

1. **音量矩阵**：20%, 50%, 80%, 100%
2. **距离矩阵**：0.5m, 1m, 2m, 3m
3. **说话人矩阵**：成年男/女、儿童

### 阶段 3：扩展场景验证（2-3 天）

1. **环境噪声**：音乐、电视、风扇
2. **方位角度**：正面、侧面、背面
3. **边界情况**：极小音量、极远距离、快速语速

### 阶段 4：长时间稳定性（1-2 天）

1. **连续对话**：100 轮、500 轮
2. **内存泄漏检查**
3. **看门狗触发检查**

## 声学能力登记

完成验收后，更新服务端声学能力登记：

```sql
INSERT INTO device_acoustic_capability (
  board_profile,
  firmware_version_range,
  acoustic_profile_version,
  simultaneous_capture_playback,
  aec_reference_type,
  aec_verified,
  max_barge_in_level,
  tested_volume_range,
  tested_distance_m,
  test_report_uri,
  approved_at
) VALUES (
  'atk-dnesp32s3-v1',
  '1.0.0-1.x.x',
  'aec-v1',
  true,
  'internal_reference',
  true,
  80,  -- 最大验证通过的音量
  '20-80',
  '0.5-2.0',
  's3://memoria-test-reports/acoustic-matrix-20260822.pdf',
  NOW()
);
```

## 失败处理

### 如果核心测试失败

1. **Far-end only 误触发率过高**
   - 检查 Reference tap 是否对齐
   - 调整 AEC 参数
   - 检查是否有硬件回声路径

2. **Double-talk WER 过高**
   - 检查 AEC 残余回声能量
   - 调整 NS（噪声抑制）参数
   - 可能需要硬件改进

3. **CPU/内存不足**
   - 降低 AFE 处理复杂度（Low Cost → Bypass）
   - 减少 LVGL 刷新率
   - 优化任务优先级

### 降级方案

如果无法通过完整矩阵，按整改方案 9.6 节降级：

```text
full_duplex_verified  (目标)
  ↓ 部分场景失败
interrupt_assist  (允许打断但不保证质量)
  ↓ AEC 完全不可用
half_duplex_safe  (回退到半双工)
```

降级后仍必须保留：
- 物理按钮立即停止
- 本地停止词
- 不播放旧 Generation
- 不把回声识别成永久记忆

## 验收标准

### 必须通过

✅ Far-end only @ 50% 音量，VAD 误触发率 < 5%  
✅ Near-end only @ 1m，ASR 字错率 < 5%  
✅ Double-talk @ 50%, 1m，ASR 字错率 < 20%  
✅ ERLE > 15 dB（一般环境）  
✅ CPU 占用 < 30%  
✅ 内存无泄漏，连续 100 轮稳定  

### 建议通过

✅ 音量 20%-80% 范围测试通过  
✅ 距离 0.5m-2m 测试通过  
✅ 儿童语音测试通过  
✅ 背景音乐环境测试通过  

### 标记为 full_duplex_verified 的条件

必须同时满足：
1. 所有"必须通过"项全部通过
2. 至少 80% "建议通过"项通过
3. 有完整测试报告和数据
4. 服务端声学能力已登记
5. Runtime Profile 包含 AEC 版本信息

## 参考

- 整改方案第 9 节：全双工声学前端
- P1-1：AEC Reference 对齐
- P1-2：本地停止词
- P1-3：语义打断分类
