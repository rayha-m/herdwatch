"""Level 2 visualisation tool: one self-contained HTML file (no external libraries).

Reads outputs/level2/brd_risk.csv + features, and the Level 1 cow-minute table, and writes
outputs/level2/dashboard.html with:
  * risk ranking (score 0-1, 0.5 = herd-typical, reliability, top reasons on hover)
  * 48-h actogram: one row per cow, one pixel column per minute, coloured by behaviour
  * per-feature strip plots: where the selected cow sits relative to the herd
  * herd feeding share over time with inferred feed deliveries
  * table view of brd_risk.csv, links to risk cards and evidence clips
    python -m pipeline.dashboard --run full
"""
import argparse, json
import numpy as np, pandas as pd
from .config import OUT

CODES = {"feeding": "F", "walking": "W", "drinking": "D", "standing": "S", "social_interaction": "X",
         "grooming": "X", "ruminating_standing": "R", "ruminating_lying": "r", "lying": "L", "other_not_visible": "."}


def build(run):
    l1 = OUT / "level1" / run; l2 = OUT / "level2"
    ml = pd.read_csv(l1 / "activity_minutes.csv", parse_dates=["minute"])
    risk = pd.read_csv(l2 / "brd_risk.csv")
    feat = pd.read_csv(l2 / "features_per_cow_day.csv")
    herd = pd.read_csv(l2 / "herd_feeding_share_by_minute.csv", parse_dates=["minute"])
    model = json.load(open(l2 / "risk_model.json"))
    t0 = ml.minute.min().floor("h"); t1 = ml.minute.max().ceil("h")
    n = int((t1 - t0).total_seconds() // 60)
    acto = {}
    for cow, g in ml.groupby("cow_id"):
        arr = np.array(["."] * n, dtype="<U1")
        idx = ((g.minute - t0).dt.total_seconds() // 60).astype(int).values
        arr[idx] = [CODES.get(b, ".") for b in g.behavior]
        acto[cow] = "".join(arr)
    hs = herd.set_index("minute").herd_feeding_share
    hser = [round(float(hs.get(t0 + pd.Timedelta(minutes=i), np.nan)), 2) if (t0 + pd.Timedelta(minutes=i)) in hs.index else None
            for i in range(0, n, 10)]
    fcols = ["feeding_min_per_day", "bunk_visits_per_day", "feedcall_attendance", "lying_pct",
             "longest_lying_bout_min", "isolation_nn_bw", "feeding_change_d2_vs_d1", "drinking_visits_per_day"]
    data = dict(t0=str(t0), minutes=n, acto=acto, herd=hser, feed_events=model["feed_events"],
                risk=json.loads(risk.to_json(orient="records")),
                features=[c for c in fcols if c in risk.columns],
                perday=json.loads(feat.to_json(orient="records", date_format="iso")))
    html = TEMPLATE.replace("/*DATA*/", json.dumps(data, separators=(",", ":"), default=str))
    (l2 / "dashboard.html").write_text(html)
    print("wrote", l2 / "dashboard.html", f"{len(html)/1e6:.2f} MB")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pen 6 BRD Risk</title>
<style>
:root{color-scheme:light;--surface:#fcfcfb;--surface-2:#f3f2ef;--grid:#e4e3df;--text:#0b0b0b;--text-2:#52514e;--muted:#8a8984;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--none:#ebeae6;
--crit:#d03b3b;--serious:#ec835a;--good:#0ca30c}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--surface:#1a1a19;--surface-2:#242422;--grid:#383835;--text:#f0efec;--text-2:#c3c2b7;--muted:#8f8e86;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--none:#2c2c2a}}
:root[data-theme="dark"]{color-scheme:dark;--surface:#1a1a19;--surface-2:#242422;--grid:#383835;--text:#f0efec;--text-2:#c3c2b7;--muted:#8f8e86;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--none:#2c2c2a}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}.sub{color:var(--text-2);margin:0 0 16px}
.card{background:var(--surface-2);border-radius:10px;padding:14px 14px 10px}
.grid2{display:grid;grid-template-columns:1.1fr 1fr;gap:16px}@media(max-width:860px){.grid2{grid-template-columns:1fr}}
.row{display:flex;align-items:center;gap:8px;padding:3px 0;cursor:pointer;border-radius:6px}.row:hover,.row.sel{background:var(--grid)}
.rk{width:22px;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums}.nm{width:52px;font-weight:600}
.track{position:relative;flex:1;height:14px}.bar{position:absolute;left:0;top:3px;height:8px;border-radius:0 4px 4px 0;background:var(--s1)}
.mid{position:absolute;top:0;bottom:0;width:1px;background:var(--muted)}.val{width:44px;text-align:right;font-variant-numeric:tabular-nums}
.tag{font-size:11px;padding:1px 6px;border-radius:9px;border:1px solid var(--grid);color:var(--text-2);white-space:nowrap}
.legend{display:flex;flex-wrap:wrap;gap:10px 14px;margin:6px 0 8px;color:var(--text-2);font-size:12px}.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
canvas{width:100%;display:block}.reasons{color:var(--text-2);font-size:13px;margin:6px 0 0}
#tip{position:fixed;pointer-events:none;background:var(--text);color:var(--surface);padding:6px 8px;border-radius:6px;font-size:12px;max-width:340px;display:none;z-index:9}
table{border-collapse:collapse;width:100%;font-size:12.5px}th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--grid);vertical-align:top}th{color:var(--text-2);font-weight:600}
.strip{display:grid;grid-template-columns:210px 1fr;gap:4px 10px;align-items:center;font-size:12.5px}.strip .lab{color:var(--text-2)}
.note{color:var(--muted);font-size:12px}a{color:var(--s1)}button{font:inherit;background:none;border:1px solid var(--grid);color:var(--text);border-radius:6px;padding:2px 8px;cursor:pointer}
.tablewrap{overflow-x:auto}
</style></head><body><main>
<h1>Pen 6: BRD risk dashboard</h1>
<p class="sub">10 Angus cows · 3 cameras · <span id="span"></span>. Risk is relative to this herd: 0.5 means herd-typical. Behaviour is observed by cameras; risk is <b>estimated</b> and is not a diagnosis.
<button id="themebtn" style="float:right">Toggle theme</button></p>
<div class="grid2">
 <div class="card"><h2 style="margin-top:0">Risk ranking</h2><div id="rank"></div>
  <p class="note">Bar = risk score; grey line = 0.5 (herd-typical). Click a cow to inspect.</p></div>
 <div class="card"><h2 style="margin-top:0" id="selname">Selected cow</h2><div id="reasons" class="reasons"></div>
  <div id="strips" class="strip" style="margin-top:10px"></div>
  <p class="note">Each dot = one cow (feature averaged over observed days). Highlighted = selected cow. Right = more BRD-like.</p></div>
</div>
<h2>48-hour actogram (one row per cow, one column per minute)</h2>
<div class="legend" id="legend"></div>
<div class="card"><canvas id="acto" height="300"></canvas><canvas id="herd" height="70"></canvas>
<p class="note">Lower strip: share of visible cows feeding (10-min means); ▲ = inferred feed delivery. Light grey cells = cow not visible on any camera.</p></div>
<h2>Table: brd_risk.csv</h2><div class="tablewrap"><table id="tbl"></table></div>
<p class="note">Risk cards and evidence clips: <a href="risk_cards/">risk_cards/</a> · clips in <a href="../evidence_clips/">evidence_clips/</a>.</p>
</main><div id="tip"></div>
<script>
const D=/*DATA*/;
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const BEH={F:['feeding','--s1'],W:['walking','--s2'],D:['drinking','--s3'],S:['standing','--s4'],X:['social / grooming','--s5'],R:['ruminating (standing)','--s6'],r:['ruminating (lying)','--s6'],L:['lying','--s7'],'.':['not visible','--none']};
const FEAT={feeding_min_per_day:['Bunk time (min/obs-day)',-1],bunk_visits_per_day:['Bunk visits (/obs-day)',-1],feedcall_attendance:['Fresh-feed response (%)',-1],
 lying_pct:['Lying (% of observed)',1],longest_lying_bout_min:['Longest lying bout (min)',1],isolation_nn_bw:['Nearest herd-mate (body widths)',1],
 feeding_change_d2_vs_d1:['Bunk time change D2 vs D1 (%)',-1],drinking_visits_per_day:['Waterer visits (/obs-day)',-1]};
const tip=document.getElementById('tip');
function showTip(e,h){tip.innerHTML=h;tip.style.display='block';const x=Math.min(e.clientX+12,innerWidth-350);tip.style.left=x+'px';tip.style.top=(e.clientY+12)+'px'}
function hideTip(){tip.style.display='none'}
const t0=new Date(D.t0.replace(' ','T'));document.getElementById('span').textContent=t0.toLocaleDateString()+' + '+Math.round(D.minutes/60)+' h';
let sel=D.risk[0].cow_id;
function tier(r){return r>=0.7?['High','--crit','▲']:r>=0.6?['Elevated','--serious','●']:['Typical','--good','✓']}
function drawRank(){const el=document.getElementById('rank');el.innerHTML='';
 D.risk.forEach(r=>{const [t,c,ic]=tier(r.risk_score);const d=document.createElement('div');d.className='row'+(r.cow_id===sel?' sel':'');
  d.innerHTML=`<span class="rk">${r.rank}</span><span class="nm">${r.cow_id}</span><span class="track"><span class="bar" style="width:${r.risk_score*100}%"></span><span class="mid" style="left:50%"></span></span><span class="val">${r.risk_score.toFixed(2)}</span><span class="tag" style="border-color:${css(c)}">${ic} ${t}</span><span class="tag" title="data reliability">data: ${r.reliability}</span>`;
  d.onclick=()=>{sel=r.cow_id;drawAll()};d.onmousemove=e=>showTip(e,`<b>${r.cow_id}</b> risk ${r.risk_score.toFixed(2)} (rank ${r.rank})<br>${r.top_reasons}<br><i>${r.observed_hours} h observed · ${r.reliability} reliability</i>`);d.onmouseleave=hideTip;el.appendChild(d)})}
function drawSel(){const r=D.risk.find(x=>x.cow_id===sel);document.getElementById('selname').textContent=`${sel}: risk ${r.risk_score.toFixed(2)}, rank ${r.rank} of ${D.risk.length}`;
 document.getElementById('reasons').textContent='Top reasons: '+r.top_reasons;const st=document.getElementById('strips');st.innerHTML='';
 D.features.forEach(f=>{const [lab,dir]=FEAT[f]||[f,1];const vals=D.risk.map(x=>[x.cow_id,x[f]]).filter(v=>v[1]!==null&&v[1]!==undefined);
  if(!vals.length)return;const xs=vals.map(v=>v[1]*dir);const lo=Math.min(...xs),hi=Math.max(...xs);const sp=(hi-lo)||1;
  const w=document.createElement('div');w.className='lab';w.textContent=lab;st.appendChild(w);
  const s=document.createElementNS('http://www.w3.org/2000/svg','svg');s.setAttribute('viewBox','0 0 300 18');s.setAttribute('width','100%');s.setAttribute('height','18');
  s.innerHTML=`<line x1="6" x2="294" y1="9" y2="9" stroke="${css('--grid')}" stroke-width="2"/>`+vals.map(([c,v])=>{const x=6+288*((v*dir-lo)/sp);const me=c===sel;
   return `<circle cx="${x}" cy="9" r="${me?6:4}" fill="${me?css('--s2'):css('--muted')}" stroke="${css('--surface-2')}" stroke-width="2"><title>${c}: ${(+v).toFixed(1)}</title></circle>`}).join('');
  st.appendChild(s)})}
function drawActo(){const cv=document.getElementById('acto');const cows=D.risk.map(r=>r.cow_id).sort();const dpr=devicePixelRatio||1;
 const W=cv.clientWidth,rowH=24,padL=58,padT=18,H=padT+rowH*cows.length+4;cv.style.height=H+'px';cv.width=W*dpr;cv.height=H*dpr;const g=cv.getContext('2d');g.scale(dpr,dpr);
 g.fillStyle=css('--surface-2');g.fillRect(0,0,W,H);const pw=(W-padL-6)/D.minutes;g.font='12px system-ui';g.textBaseline='middle';
 const col={};for(const k in BEH)col[k]=css(BEH[k][1]);
 cows.forEach((c,i)=>{const y=padT+i*rowH;g.fillStyle=c===sel?css('--text'):css('--text-2');g.fillText(c,6,y+rowH/2);const s=D.acto[c]||'';
  let run=s[0],st=0;for(let m=1;m<=s.length;m++){if(m===s.length||s[m]!==run){g.fillStyle=col[run]||col['.'];g.fillRect(padL+st*pw,y+3,Math.max((m-st)*pw,0.6),rowH-6);run=s[m];st=m}}
  if(c===sel){g.strokeStyle=css('--text');g.lineWidth=1.5;g.strokeRect(padL-1,y+2,W-padL-5,rowH-4)}});
 g.fillStyle=css('--muted');for(let h=0;h<=D.minutes/60;h+=6){const x=padL+h*60*pw;g.fillRect(x,padT-4,1,H-padT);const d=new Date(t0.getTime()+h*3600e3);g.fillText(d.getHours().toString().padStart(2,'0')+':00'+(d.getHours()===0?' '+d.toLocaleDateString(undefined,{weekday:'short'}):''),x+3,8)}
 cv.onmousemove=e=>{const r=cv.getBoundingClientRect();const x=e.clientX-r.left,y=e.clientY-r.top;const i=Math.floor((y-padT)/rowH),m=Math.floor((x-padL)/pw);
  if(i<0||i>=cows.length||m<0||m>=D.minutes)return hideTip();const k=(D.acto[cows[i]]||'')[m]||'.';const t=new Date(t0.getTime()+m*60e3);
  showTip(e,`<b>${cows[i]}</b> ${t.toLocaleString()}<br>${BEH[k][0]}`)};cv.onmouseleave=hideTip;cv.onclick=e=>{const r=cv.getBoundingClientRect();const i=Math.floor((e.clientY-r.top-padT)/rowH);if(cows[i]){sel=cows[i];drawAll()}}
 const hv=document.getElementById('herd');const H2=70;hv.style.height=H2+'px';hv.width=W*dpr;hv.height=H2*dpr;const h=hv.getContext('2d');h.scale(dpr,dpr);h.fillStyle=css('--surface-2');h.fillRect(0,0,W,H2);
 const n=D.herd.length,bw=(W-padL-6)/n;h.fillStyle=css('--s1');D.herd.forEach((v,i)=>{if(v===null)return;const bh=v*(H2-22);h.fillRect(padL+i*bw,H2-6-bh,Math.max(bw-0.5,0.5),bh)});
 h.fillStyle=css('--text-2');h.font='11px system-ui';h.textBaseline='top';h.fillText('herd',6,8);h.fillText('feeding',6,22);
 D.feed_events.forEach(ev=>{const m=(new Date(ev.replace(' ','T'))-t0)/60e3;const x=padL+m*pw;h.fillStyle=css('--text');h.beginPath();h.moveTo(x,4);h.lineTo(x-5,13);h.lineTo(x+5,13);h.fill()})}
function drawLegend(){const seen={};document.getElementById('legend').innerHTML=Object.keys(BEH).filter(k=>{const n=BEH[k][0].split(' (')[0];if(seen[BEH[k][1]+n])return false;seen[BEH[k][1]+n]=1;return true}).map(k=>`<span><span class="sw" style="background:${css(BEH[k][1])}"></span>${k==='R'?'ruminating':BEH[k][0]}</span>`).join('')}
function drawTable(){const cols=['rank','cow_id','risk_score','reliability','observed_hours','top_reasons'];const t=document.getElementById('tbl');
 t.innerHTML='<tr>'+cols.map(c=>`<th>${c}</th>`).join('')+'</tr>'+D.risk.map(r=>'<tr>'+cols.map(c=>`<td>${typeof r[c]==='number'?(+r[c]).toFixed(c==='risk_score'?3:1).replace(/\.0$/,''):r[c]}</td>`).join('')+'</tr>').join('')}
function drawAll(){drawRank();drawSel();drawActo();drawLegend();drawTable()}
document.getElementById('themebtn').onclick=()=>{const r=document.documentElement;const dark=r.dataset.theme?r.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;r.dataset.theme=dark?'light':'dark';drawAll()};
addEventListener('resize',drawActo);drawAll();
</script></body></html>"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--run", default="full"); a = ap.parse_args()
    build(a.run)
