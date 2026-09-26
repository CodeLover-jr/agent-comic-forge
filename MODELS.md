# 模型清单（阿里云百炼）

> 本项目所有模型均来自**阿里云百炼（Model Studio / DashScope）**，共用一个 `DASHSCOPE_API_KEY`。
> **以后更换模型时，请同步更新本表。**
> 模型默认值在 `config.py`，可在 `.env` 中用同名变量覆盖。

| # | 配置变量 | 当前模型 | 用途 | 对应 Agent |
|---|---|---|---|---|
| 1 | `QWEN_MODEL` | `qwen3.8-flash` | 文本大模型：任务编排、剧情扩写、分镜剧本（必须支持 Function Calling） | 编排 / 编剧 |
| 2 | `IMAGE_MODEL` | `qwen-image-3.0` | 文生图：生成每个镜头的关键帧（multimodal-generation 端点） | 画师 |
| 3 | `QWEN_VL_MODEL` | `qwen3.8-omni-flash` | 全模态视觉理解（图生文）：检查关键帧角色是否一致、画风是否统一（OpenAI兼容 chat 端点） | 画师自检 |
| 4 | `VIDEO_MODEL` | `wan3.0-video`（标准版） | 图生视频：关键帧 → 3~5 秒视频片段（异步任务）。prime 免费额度耗尽后切换，API 相同 | 画师 |
| 5 | `TTS_MODEL` | `qwen-audio-3.1-tts-flash` | 语音合成：为台词 / 旁白生成配音 | 剪辑 |

## TTS 音色

| 配置变量 | 当前音色 | 用途 |
|---|---|---|
| `TTS_VOICE_MALE` | `longanyang_v3.1`（龙安洋） | 阳光大男孩 |
| `TTS_VOICE_FEMALE` | `longanhuan_v3.1`（龙安欢） | 欢脱元气女 |
| `TTS_VOICE_NARRATOR` | `anmingyuan_v3.1`（安明远） | 旁白 |

## 更换模型注意事项

- 模型 ID 以百炼「模型广场」实际展示为准；报"模型不存在"时到广场复制最新 ID。
- 文本模型必须支持 **Function Calling**，否则编排会降级为固定顺序执行。
- 图生视频首帧不能用 Base64（会被计费网关拦截），需走临时存储上传（`tools/upload.py`）。
- 文生图走 DashScope 原生 `multimodal-generation` 端点（`tools/t2i.py`），不是 OpenAI images 端点。
- 视觉自检（omni）和 TTS 必须走「业务空间专属 host」（`config.OMNI_COMPAT_BASE` / `config.TTS_BASE`）；
  模型广场对应模型的「API代码示例」里可复制该 host，换业务空间后需同步修改。
- TTS 引擎偶发 `Engine error 411`（多为瞬时/请求过密），已内置重试，失败镜头用静音占位不中断。
