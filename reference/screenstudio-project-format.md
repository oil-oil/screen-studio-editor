# Screen Studio 工程文件结构参考

`.screenstudio` 是一个目录 bundle(在 Finder 里表现为单个文件)。本文档记录其内部结构,以及如何在不破坏时间轴的前提下**替换录屏画面层**、**手动编排点击放大**。字段名保留英文原样。

---

## 1. Bundle 目录结构

```
Xxx.screenstudio/
├── project.json              渲染剧本(编辑器状态) —— 决定成片长什么样
├── project.json.bak          本 skill 首次运行时的备份
├── meta.json                 工程级元信息(缩略图、时长等)
├── recording-markers.json    录制打点
├── .autoedit-state.json      自动剪辑状态
├── transcript.json           转写结果(经本 skill 处理后才有)
└── recording/                原始录制素材(多通道)
    ├── metadata.json         录制元数据 —— 定义有哪些通道、每段录了多久
    ├── channel-2-display-0.m3u8            录屏 session 0 播放列表
    ├── channel-2-display-0-0000.mp4        录屏 session 0 的 fMP4 init 段
    ├── channel-2-display-0-0001.m4s …      录屏 session 0 媒体分片
    ├── channel-4-webcam-0.m3u8 / *.m4s     摄像头
    ├── channel-3-microphone-0.m4a / *.m4s  麦克风
    ├── channel-1-system-audio-0.* …        系统声音
    ├── keystrokes-0.json / mouseclicks-0.json / mousemoves-0.json  键鼠轨迹
    └── …(每个通道每个 session 一套)
```

关键心智模型:**一个工程 = 几条各自独立的素材通道(录屏 / 摄像头 / 麦克风 / 系统声音 / 键鼠) + 一份纯 JSON 的渲染剧本 `project.json`。** 录屏、摄像头各是单独一层,放大只是剧本里的一段数据。所以「把录屏换成别的画面」= 只替换 display 这一条通道的分片,其余不动。

---

## 2. project.json

顶层两个键:`{ "json": {...}, "meta": {...} }`。渲染剧本全在 `json` 下。

```
json
├── id / name / createdAt / updatedAt / lastSavedAt
├── config      全局渲染样式(见 2.1)
├── scenes[]    场景,通常只有 1 个(见 2.2)
└── meta
```

### 2.1 json.config —— 全局渲染样式(纯参数,可直接改)

按主题分组的关键字段:

- **输出画布**:`defaultOutputAspectRatio`({x,y},如 `{4,3}`)、`backgroundPaddingRatio`(背景留白,如 1.02)、`windowBorderRadius`(录屏窗口圆角,如 25)、`backgroundType`(`system`/`color`/`image`…)、`backgroundColor`、`backgroundImage`、`backgroundBlur`、`backgroundGradient`
- **摄像头**:`hideCamera`(bool)、`cameraSize`(0~1,如 0.3)、`cameraPosition`(`top-right` 等)、`cameraPositionPoint`({x,y})、`cameraAspectRatio`(`square`/`wide`…)、`cameraRoundness`(0~1)、`mirrorCamera`、`cameraScaleDuringZoom`
- **光标 / 点击**:`cursorSize`、`cursorSet`、`hideCursor`、`clickEffect`、`clickSoundEffect`、`clickSoundEffectVolume`、`mouseClickSpring`
- **运动 / 弹簧**:`mouseMovementSpring`、`screenMovementSpring`、`motionBlurAmount` 及一组 `motionBlur*`
- **阴影**:`shadowIntensity` / `shadowAngle` / `shadowDistance` / `shadowBlur` / `shadowIsDirectional`
- **音频**:`audioVolume`、`muteMicrophone`、`muteSystemAudio`、`improveMicrophoneAudio`
- **其它**:`showTranscript`、`showShortcuts`、`alwaysKeepZoomedIn`、`deviceFrameKey`、`enableDeviceMockup`、`recordingRange`、`recordingCrop`

> 本 skill 的 `process.py` 会自动写入的一组默认:4:3 输出、2% padding、25 圆角、摄像头 30% 方形右上、麦克风降噪+归一。

### 2.2 json.scenes[0]

```
scenes[0]
├── id / name / type / sessionIndex
├── slices[]       时间轴切片(见 2.3)
├── zoomRanges[]   点击放大效果(见 2.4)★
├── layouts[]      布局覆写(通常空)
├── masks[]        遮罩
└── resolvedTypingSpeedIncreaseSuggestions
```

### 2.3 slices[] —— 时间轴切片

每段字段:`id`、`timeScale`、`sourceStartMs`、`sourceEndMs`、`volume`、`systemAudioVolume`、`hideCursor`、`disableSmoothMouseMovement`、`externalDeviceAudioVolume`。

语义:一段 slice 就是「从**源时间轴**截取 `sourceStartMs → sourceEndMs` 这一段放进成片」。剪掉停顿 = 删掉或缩短某些 slice。所有 slice 的时长之和 = 成片时长。

**源时间轴 = 各 session 首尾相接拼成的连续毫秒轴**(见第 3 节多 session 说明)。所以替换录屏画面时,只要保持每个 session 的时长不变,slice 的 `sourceStartMs/EndMs` 就仍然指向同一时刻,**不需要改动 slices**。

### 2.4 zoomRanges[] —— 点击放大 ★

每个放大区间是一段纯 JSON,可完全用代码生成:

```json
{
  "id": "U4v5b8GnCx",
  "zoom": 1.196,                     // 放大倍数
  "type": "follow-click-groups",     // 放大方式,见下
  "manualTargetPoint": {"x":0.5,"y":0.5}, // 手动放大时的中心点(画面归一化坐标)
  "snapToEdgesRatio": 0.25,
  "glideDirection": null,
  "glideSpeed": 0.5,
  "isDisabled": false,               // 置 true 即禁用该段
  "startTime": 14581,                // 起点,毫秒
  "endTime": 17476,                  // 终点,毫秒
  "isSystem": false
}
```

- `type` **恒为 `"follow-click-groups"`**——手动加的放大也是这个 type,SS 不靠 type 区分手动/自动。规则是:**有鼠标点击就跟点击放大;没有点击(纯口播、或替换成 PPT 后)就退回用 `manualTargetPoint` 当放大中心。** 所以替换成 PPT 后做手动放大,`type` 保持不动,只改 `manualTargetPoint` + `zoom` + `startTime`/`endTime`。
- `manualTargetPoint`:`{x,y}` 画面归一化坐标,放大聚焦到这里;`zoom`:放大倍数(如 1.25)。
- `startTime`/`endTime` 单位毫秒,落在**编辑后的预览时间轴**(用户在 SS 编辑器里看到的时间,即成片时间轴,和字幕、翻页同一条轴)。

---

## 3. recording/metadata.json

```
metadata
├── polyrecorderVersion
├── recorders[]   每条通道一个(见下)
├── sessions[]    顶层 session(部分工程有)
└── state
```

每个 recorder:`{ id, type, sessions[] }`,`type` 取值:

| type | 说明 | 素材文件 |
|------|------|----------|
| `display` | 录屏 | `channel-N-display-*.mp4/m4s/m3u8` |
| `webcam` | 摄像头 | `channel-N-webcam-*` |
| `microphone` | 麦克风 | `channel-N-microphone-*.m4a` |
| `systemAudio` | 系统声音 | `channel-N-system-audio-*` |
| `input` | 键鼠 | `keystrokes-N.json` 等 |
| `cursor` | 光标 | (常无 session) |

**display session 字段**:`bounds`({x,y,width,height})、`recordingScale`、`displayRefreshRate`、`durationMs`、`outputFilename`、`processTimeStartMs/EndMs`、`unixStartMs/EndMs`。

- **实际编码像素 = bounds ÷ recordingScale**。例:bounds `1289×840` 且 scale `0.5` → 分片实际是 `2578×1680`。替换画面时按这个像素尺寸生成,`bounds`/`recordingScale` 就无需改。
- `outputFilename` 形如 `channel-2-display-0.mp4`,但磁盘上实际是同名前缀的 `.m3u8` + 分片;SS 用 `channel-<n>-<type>-<session>` 前缀去找对应 m3u8。

**多 session**:录制中途暂停 / 继续会产生多个 session(如 display 有 session 0 和 session 1)。它们在源时间轴上**首尾相接连续拼接**:session 0 覆盖 `0 → dur0`,session 1 接着覆盖 `dur0 → dur0+dur1`。slice 的 source 时间就落在这条拼接轴上。替换画面必须逐 session 对应、逐 session 保持时长。

---

## 4. 录制素材:HLS fMP4 分片

display / webcam 每个 session 是一套标准的 **HLS fMP4 VOD**(不是私有封装):

- `channel-<n>-<type>-<sess>-0000.mp4` —— init 段(m3u8 里的 `#EXT-X-MAP`)
- `channel-<n>-<type>-<sess>-0001.m4s …` —— 媒体分片,编号**从 0001 起**
- `channel-<n>-<type>-<sess>.m3u8` —— VOD 播放列表

视频编码:`h264` / `avc1` / `yuv420p` / `bt709`。音频:`m4a`。m3u8 头部特征:`#EXT-X-VERSION:6`、`#EXT-X-PLAYLIST-TYPE:VOD`、`#EXT-X-INDEPENDENT-SEGMENTS`、`#EXT-X-MAP:URI=…`、结尾 `#EXT-X-ENDLIST`。

---

## 5. 替换录屏(display)画面层

目标:把 display 通道的画面换成任意视频(如一份 PPT 的录屏),让 SS 照常套用背景 / 圆角 / 摄像头浮层 / 放大来渲染。

**永远在克隆副本上操作**(APFS 秒级克隆、不占额外空间、原件只读):

```bash
cp -Rc "原工程.screenstudio" "副本.screenstudio"
```

步骤:

1. 从 metadata 读出 display 每个 session 的 `durationMs` 和目标像素尺寸(`bounds ÷ recordingScale`)。
2. 把替换视频做成该像素尺寸(比例不符时 letterbox 补边;PPT 白底时白边不可见)。
3. 按各 session 的 `durationMs` 把视频切成对应的几段。
4. 每段用 ffmpeg 生成 fMP4 HLS 分片,命名严格匹配原前缀:

   ```bash
   ffmpeg -ss <START> -t <DUR> -i full.mp4 \
     -c:v libx264 -crf 20 -pix_fmt yuv420p -g 60 \
     -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
     -f hls -hls_time 2 -hls_segment_type fmp4 \
     -hls_playlist_type vod -hls_list_size 0 -hls_flags independent_segments \
     -hls_fmp4_init_filename "channel-2-display-<S>-0000.mp4" \
     -hls_segment_filename   "channel-2-display-<S>-%04d.m4s" \
     -start_number 1 \
     "channel-2-display-<S>.m3u8"
   ```

5. 删除副本 `recording/` 里原 `channel-2-display-*`,放入新分片与 m3u8。
6. 把生成的 m3u8 里的 `#EXT-X-VERSION:7` 改回 `6`,贴近原始。
7. 保持像素尺寸与各 session 时长不变时,`metadata.json` 与 `project.json` 的 slices **都无需改动**。

自检:`ffprobe channel-2-display-0.m3u8` 时长、尺寸对得上;webcam / 音频分片数不变。最终「SS 认不认」必须在 Screen Studio 里打开工程确认(它是闭源应用,只能由它渲染 / 导出)。

**注意点**:

- **display 通道同时存在两套源,必须都替换**:一套是**完整 mp4**(`channel-2-display-<S>.mp4`,即 metadata 里 session 的 `outputFilename`,通常几百 MB),另一套是 **HLS 分片**(`channel-2-display-<S>-0000.mp4` + `-NNNN.m4s` + `.m3u8`)。**Screen Studio 优先读完整 mp4**——只换分片、不换完整 mp4,打开后画面看起来毫无变化。注意完整 mp4 的文件名**不带** session 之后的 `-NNNN`,用 `ls channel-2-display-*` 时容易和分片混淆而漏掉。
- **Screen Studio 运行时会把 display 缓存在内存**:替换磁盘文件后,只在应用内关窗重开工程往往仍显示旧画面,需**完全退出(Cmd+Q)再重新打开**工程才会刷新。
- display 若有多个 session,要逐个替换,总画面在源时间轴上保持连续。
- 原录屏的 `zoomRanges` 多为 `follow-click-groups`,针对旧鼠标轨迹;换成 PPT 后这些放大会落在无意义的位置,需按第 6 节改为手动放大,或先 `isDisabled: true` 关掉。
- 若替换视频的比例 / 尺寸与原 display 不同而选择改 `bounds`,会牵动 SS 的布局计算,风险高;优先用「补边到原尺寸」避免改 metadata。

---

## 6. 让 PPT 翻页对齐口播

替换进去的 PPT 要「讲到哪页显示哪页」,得把翻页时刻对齐到源时间轴:

1. 转写口播(成片音频),定出每页的成片起始时刻(讲到该页内容的时间)。
2. 用 `slices` 把「成片时刻」换算成「源时刻」:每个 slice 的成片区间 ↔ 源区间线性对应(`timeScale=1` 时就是平移,成片时长 = `(sourceEndMs-sourceStartMs)/timeScale`)。
3. 在源时间轴上采样,每个源时刻反查落在哪个 slice → 对应成片时刻 → 该显示哪页;被剪掉的源区间(slice 之间)填相邻页(反正不出现在成片)。
4. 按这个「源时间轴页序列」合成源视频,再逐 session 切分替换(见第 5 节)。

关键:改的是**源**画面,翻页表却是按**成片**时间定的,`slices` 就是两者之间的桥。

## 7. 让放大跟着讲述走 + 隐藏鼠标

替换成 PPT 后,原来的 `zoomRanges` 是跟旧鼠标点击的,落在 PPT 上会乱放大——先全部 `isDisabled: true` 关掉(或清空),再按需要生成新的。

每个手动放大就是一段 zoomRange:**`type` 保持 `"follow-click-groups"`**(见 2.4——没有点击时它就用 `manualTargetPoint` 当放大中心)、`manualTargetPoint` 指到该页要聚焦的内容、`zoom` 给倍数、`startTime`/`endTime` 用成片时间轴毫秒卡住放大时段。要让放大对齐「讲到哪放大哪」,先转写口播拿到每段话的成片时刻,再决定哪个时刻放大到画面哪块,逐段生成即可。

坐标系:`manualTargetPoint` 的 `{x,y}` 是归一化坐标,`0.5,0.5` 是画面正中。具体边界(相对整个输出画布还是录屏内容区)最好先放一个测试放大、在 SS 里看焦点落点来校准,再批量生成。

**隐藏鼠标**:`config.hideCursor = true` 全局隐藏合成鼠标(PPT 演示不需要指针)。SS 保存工程后会保留这个设置。
