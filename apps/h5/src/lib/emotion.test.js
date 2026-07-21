import { describe, expect, it } from "vitest";

import { classifyEmotion, emotionFromVoice } from "./emotion.js";

describe("classifyEmotion", () => {
  it.each([
    ["今天真的很开心，谢谢你", "happy"],
    ["这件事太不公平了，我很生气", "upset"],
    ["为什么会这样？", "curious"],
    ["今天完成了一次散步", "neutral"],
  ])("maps %s to %s", (text, expected) => {
    expect(classifyEmotion(text)).toBe(expected);
  });

  it("keeps priority deterministic when several cues appear", () => {
    expect(classifyEmotion("虽然有点疑惑，但我真的很开心")).toBe("happy");
  });

  it.each([
    ["happy", "happy"],
    ["surprised", "curious"],
    ["sad", "caring"],
    ["angry", "caring"],
    ["fearful", "caring"],
    ["disgusted", "caring"],
    ["neutral", "neutral"],
    ["unknown", "neutral"],
  ])("maps conservative voice label %s to %s", (label, expected) => {
    expect(emotionFromVoice(label)).toBe(expected);
  });
});
