#!/usr/bin/env python3
"""
纯口播工程 → 自动配 PPT：把渲染好的 deck 页按口播翻页对齐、加淡入动画，
替换进 Screen Studio 工程的 display，并按需改竖屏 bounds、隐藏鼠标、编排放大。

Agent 负责前半段（转写、设计 deck、渲染页图、定翻页表和放大点），
本脚本负责机械的后半段（对齐 → 合成 → 切 session → 替换 → 改 project.json）。

用法:
  auto_ppt_replace.py \
    --project "/path/Xxx.screenstudio" \
    --pages   "/path/rendered_pages"           # 内含 deck00.png .. deckNN.png（页 = index+1）
    --plan    "/path/plan.json"                 # 翻页表 + 放大点 + 画幅，见下

plan.json:
  {
    "orientation": "portrait",                  # portrait(3:4) | landscape(原比例)
    "width": 1260, "height": 1680,              # deck 页图的像素尺寸
    "fade": 0.45,                               # 每页淡入时长(秒)
    "hide_cursor": true,
    "page_starts": [[0,1],[6.7,2],[18,3], ...], # [成片秒, 页号(1based)]
    "zooms": [                                  # 放大点(成片时间轴)，可空
      {"comp_start": 68, "comp_end": 74, "x": 0.5, "y": 0.6, "zoom": 1.2}
    ]
  }
永远在克隆副本上跑（cp -Rc）。原件只读。
"""
import argparse, json, subprocess, os, glob, string, random

def run(c): subprocess.run(c, shell=True, check=True)
def probe(f): return float(subprocess.check_output(
    f'ffprobe -v error -show_entries format=duration -of csv=p=0 "{f}"', shell=True))
def rid(k=10): return "".join(random.choices(string.ascii_letters+string.digits, k=k))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--pages", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--work", default=None, help="中间产物目录(默认工程旁 .autoppt)")
    a=ap.parse_args()
    proj=a.project.rstrip("/"); rec=proj+"/recording"
    plan=json.load(open(a.plan))
    W,H=plan["width"], plan["height"]; fade=plan.get("fade",0.45)
    work=a.work or (proj+"/.autoppt"); os.makedirs(work, exist_ok=True)

    # display session 时长 + scale（从 metadata）
    meta=json.load(open(rec+"/metadata.json"))
    disp=next(r for r in meta["recorders"] if r["type"]=="display")
    sess=sorted(disp["sessions"], key=lambda s:s["processTimeStartMs"])
    durs=[s["durationMs"]/1000.0 for s in sess]; scale=sess[0].get("recordingScale",0.5)
    SRC_TOTAL=sum(durs)

    # slices：成片 <-> 源（timeScale 若非 1 一并处理）
    slices=json.load(open(proj+"/project.json"))["json"]["scenes"][0]["slices"]
    segs=[]; ct=0.0
    for s in slices:
        ts=s.get("timeScale",1) or 1
        L=(s["sourceEndMs"]-s["sourceStartMs"])/1000.0/ts
        segs.append((ct, ct+L, s["sourceStartMs"]/1000.0)); ct+=L

    ps=sorted(plan["page_starts"])
    def page_at_comp(t):
        p=ps[0][1]
        for cs,pg in ps:
            if t>=cs-1e-9: p=pg
        return p
    def src_to_page(st):
        for i,(cs,ce,ss) in enumerate(segs):
            if ss-1e-9<=st<ss+(ce-cs): return page_at_comp(cs+(st-ss))
        prev=None
        for cs,ce,ss in segs:
            if ss+(ce-cs)<=st: prev=ce
        return page_at_comp((prev if prev is not None else 0)-1e-6)

    # 源时间轴分页序列
    seq=[]; t=0.0; cur=src_to_page(0.0); start=0.0
    while t<SRC_TOTAL:
        p=src_to_page(t)
        if p!=cur: seq.append((start,t-start,cur)); cur=p; start=t
        t=round(t+0.1,3)
    seq.append((start, SRC_TOTAL-start, cur))
    print("源时间轴分页:"); [print(f"  {s:6.1f}s +{d:5.1f}s -> 页{p}") for s,d,p in seq]

    # 合成：每页从白底淡入 → concat
    V=f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:white"
    segf=[]
    for i,(s,d,p) in enumerate(seq):
        o=f"{work}/seg_{i:03d}.mp4"
        run(f'ffmpeg -y -loop 1 -t {d:.3f} -i "{a.pages}/deck{p-1:02d}.png" '
            f'-vf "{V},fade=t=in:st=0:d={fade}:color=white,fps=30,format=yuv420p" '
            f'-c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p '
            f'-colorspace bt709 -color_primaries bt709 -color_trc bt709 "{o}"'); segf.append(o)
    lst=f"{work}/list.txt"; open(lst,"w").write("\n".join(f"file '{x}'" for x in segf))
    full=f"{work}/aligned_full.mp4"
    run(f'ffmpeg -y -f concat -safe 0 -i "{lst}" -c copy "{full}"')
    print(f"✅ 对齐+动画源视频: {probe(full):.2f}s (目标 {SRC_TOTAL:.2f}s)")

    # 逐 session 切 → 完整 mp4 + HLS 分片，替换
    off=0.0
    for i,dd in enumerate(durs):
        run(f'ffmpeg -y -ss {off} -t {dd} -i "{full}" -c:v libx264 -crf 20 -pix_fmt yuv420p -r 30 '
            f'-colorspace bt709 -color_primaries bt709 -color_trc bt709 -movflags +faststart "{work}/disp-{i}.mp4"')
        sd=f"{work}/seg{i}"; os.makedirs(sd, exist_ok=True); [os.remove(x) for x in glob.glob(sd+"/*")]
        run(f'cd "{sd}" && ffmpeg -y -ss {off} -t {dd} -i "{full}" -c:v libx264 -crf 20 -pix_fmt yuv420p -g 60 '
            f'-colorspace bt709 -color_primaries bt709 -color_trc bt709 '
            f'-f hls -hls_time 2 -hls_segment_type fmp4 -hls_playlist_type vod -hls_list_size 0 -hls_flags independent_segments '
            f'-hls_fmp4_init_filename "{disp["id"]}-{i}-0000.mp4" '
            f'-hls_segment_filename "{disp["id"]}-{i}-%04d.m4s" -start_number 1 "{disp["id"]}-{i}.m3u8"')
        off+=dd
    for i in range(len(durs)):
        pre=f'{disp["id"]}-{i}'
        for x in glob.glob(f"{rec}/{pre}-*")+glob.glob(f"{rec}/{pre}.m3u8")+[f"{rec}/{pre}.mp4"]:
            if os.path.exists(x): os.remove(x)
        for x in os.listdir(f"{work}/seg{i}"): run(f'cp "{work}/seg{i}/{x}" "{rec}/{x}"')
        m3=f"{rec}/{pre}.m3u8"; open(m3,"w").write(open(m3).read().replace("#EXT-X-VERSION:7","#EXT-X-VERSION:6"))
        run(f'cp "{work}/disp-{i}.mp4" "{rec}/{pre}.mp4"')
    print(f"✅ 已替换 display（{len(durs)} 个 session）")

    # 竖屏：把 bounds 改成 deck 比例，SS 才会填满竖画布
    if plan.get("orientation")=="portrait":
        for s in disp["sessions"]:
            s["bounds"]["width"]=round(W*scale); s["bounds"]["height"]=round(H*scale)
        json.dump(meta, open(rec+"/metadata.json","w"), ensure_ascii=False, separators=(",",":"))
        print(f'✅ bounds 改为 {round(W*scale)}x{round(H*scale)}（{W}:{H}）')

    # project.json：隐藏鼠标 + 写放大（放大恒用 follow-click-groups + manualTargetPoint）
    pj=proj+"/project.json"; d=json.load(open(pj))
    if plan.get("hide_cursor"): d["json"]["config"]["hideCursor"]=True
    zooms=plan.get("zooms") or []
    zr=[{"id":rid(),"zoom":z.get("zoom",1.2),"type":"follow-click-groups","snapToEdgesRatio":0.25,
         "manualTargetPoint":{"x":z["x"],"y":z["y"]},"glideDirection":None,"glideSpeed":0.5,
         "isDisabled":False,"startTime":z["comp_start"]*1000.0,"endTime":z["comp_end"]*1000.0,"isSystem":False}
        for z in zooms]
    d["json"]["scenes"][0]["zoomRanges"]=zr
    json.dump(d, open(pj,"w"), ensure_ascii=False, separators=(",",":"))
    print(f"✅ hideCursor={plan.get('hide_cursor',False)} ; 写入 {len(zr)} 个 1.2x 放大")
    print("完成。请完全退出 Screen Studio 再重开工程查看。")

if __name__=="__main__": main()
