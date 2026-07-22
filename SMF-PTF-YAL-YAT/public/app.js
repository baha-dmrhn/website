const $ = id => document.getElementById(id);
let chart, quantityChart, nextDayPtfChart, currentRows = [], compareRows = [], currentDate, compareDateValue = '', compareLabel = '', currentData = {}, comparisonData = {}, nextDayPtfData = {};
let nextDayPtfRefreshTimer = 0;
const marketConnection={main:'idle',next:'idle',mainDetail:'',nextDetail:''};
let currentCurrency = localStorage.getItem('baha-market-currency') || 'TRY';
const SUITE_FONT = 'DM Sans, Inter, system-ui, sans-serif';
const API_BASE_URL = String(window.BAHA_CONFIG?.apiBaseUrl || '').replace(/\/$/, '');
const DATA_CACHE_PREFIX='baha-market-v4:';
const CURRENCIES={
  TRY:{label:'TL',unit:'TL/MWh',icon:'₺'},
  EUR:{label:'EUR',unit:'EUR/MWh',icon:'€'},
  USD:{label:'USD',unit:'USD/MWh',icon:'$'}
};
function supportsCurrencyPayload(data){return data?.currencyInfo?.mode==='epias-ptf-direct'}
function readDataCache(date){try{const cached=JSON.parse(localStorage.getItem(DATA_CACHE_PREFIX+date));if(cached?.expires>Date.now()&&supportsCurrencyPayload(cached.data))return cached.data;localStorage.removeItem(DATA_CACHE_PREFIX+date)}catch{}return null}
function writeDataCache(date,data){try{const ttl=date===todayTR()?2*60*1000:7*24*60*60*1000;localStorage.setItem(DATA_CACHE_PREFIX+date,JSON.stringify({expires:Date.now()+ttl,data}))}catch{}}
const systemDark=window.matchMedia('(prefers-color-scheme: dark)');
function applyTheme(theme){document.documentElement.dataset.theme=theme;const dark=theme==='dark';$('theme-toggle').textContent=dark?'☀':'☾';$('theme-toggle').setAttribute('aria-label',dark?'Açık temaya geç':'Koyu temaya geç')}
function selectedTheme(){return localStorage.getItem('baha-theme')||(systemDark.matches?'dark':'light')}
function priceValue(row,key){if(key==='smf')return Number.isFinite(row?.smf)?row.smf:null;const value=row?.ptfByCurrency?.[currentCurrency];return Number.isFinite(value)?value:(currentCurrency==='TRY'&&Number.isFinite(row?.ptf)?row.ptf:null)}
function priceValues(rows,key){return rows.map(row=>priceValue(row,key)).filter(Number.isFinite)}
function priceAverage(rows,key){const values=priceValues(rows,key);return values.length?values.reduce((sum,value)=>sum+value,0)/values.length:null}
function priceSummary(data,key,rows){if(key==='smf'){const value=data?.summary?.smfAverageByCurrency?.TRY??data?.summary?.smfAverage;return Number.isFinite(value)?value:priceAverage(rows,key)}const value=data?.summary?.ptfAverageByCurrency?.[currentCurrency];return Number.isFinite(value)?value:priceAverage(rows,key)}
function currencyUnit(){return CURRENCIES[currentCurrency]?.unit||CURRENCIES.TRY.unit}
function priceUnit(key){return key==='smf'?CURRENCIES.TRY.unit:currencyUnit()}
function showHourDetail(index){const row=currentRows[index];if(!row)return;$('hour-detail-title').textContent=`${currentDate.split('-').reverse().join('.')} · ${row.time}`;$('detail-ptf').textContent=fmt(priceValue(row,'ptf'));$('detail-smf').textContent=fmt(priceValue(row,'smf'));$('detail-yal').textContent=fmt(row.yal);$('detail-yat').textContent=fmt(Number.isFinite(row.yat)?Math.abs(row.yat):null);const type=directionType(row.direction);const direction=$('detail-direction');direction.textContent=row.direction||'Veri yok';direction.className=`detail-direction ${type}`;$('hour-detail').classList.add('open');$('hour-detail-backdrop').classList.add('open');document.body.classList.add('sheet-open')}
function closeHourDetail(){$('hour-detail').classList.remove('open');$('hour-detail-backdrop').classList.remove('open');document.body.classList.remove('sheet-open')}
const fmt = value => value == null ? '—' : new Intl.NumberFormat('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:2}).format(value);
const todayTR = () => new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Istanbul'}).format(new Date());
const marketDateLabel=value=>value?new Intl.DateTimeFormat('tr-TR',{day:'2-digit',month:'long',year:'numeric',timeZone:'Europe/Istanbul'}).format(new Date(`${value}T12:00:00+03:00`)):'—';

async function api(url, options) {
  let response;
  try { response = await fetch(`${API_BASE_URL}${url}`, { credentials:'include', headers: {'Content-Type':'application/json'}, ...options }); }
  catch { throw new Error('İnternet bağlantısı yok veya sunucuya ulaşılamıyor.'); }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(body.error || 'İşlem başarısız.'), {status:response.status});
  return body;
}

function show(view) { $('login-view').classList.toggle('d-none', view !== 'login'); $('app-view').classList.toggle('d-none', view !== 'app'); document.body.classList.toggle('auth-open', view === 'login'); }
function alertAt(id, message) { const el=$(id); el.textContent=message || ''; el.classList.toggle('d-none', !message); }
function setMarketConnectionState(state='live',detail='') { const status=$('connection-status');status.classList.remove('loading','warning','error');if(state!=='live')status.classList.add(state);const label=status.querySelector('span');label.textContent=state==='error'?'EPİAŞ · Bağlantı hatası':state==='warning'?'EPİAŞ · Eksik veri':state==='loading'?'EPİAŞ · Veriler alınıyor':'EPİAŞ · EPİAŞ canlı';status.title=detail||label.textContent; }
function setMarketConnectionPart(part,state,detail=''){marketConnection[part]=state;marketConnection[`${part}Detail`]=detail;const details=[marketConnection.mainDetail,marketConnection.nextDetail].filter(Boolean).join('\n');if(marketConnection.main==='error')setMarketConnectionState('error',details);else if(marketConnection.main==='warning'||marketConnection.next==='warning'||marketConnection.next==='error')setMarketConnectionState('warning',details);else if(marketConnection.main==='loading'||marketConnection.next==='loading'||marketConnection.main==='idle'||marketConnection.next==='idle')setMarketConnectionState('loading',details);else setMarketConnectionState('live',details)}
function setMarketEmptyState(id,message){const target=$(id),empty=document.createElement('div');empty.className='chart-loading';empty.textContent=message;target.replaceChildren(empty)}
function clearMarketDashboard(message='Veri alınamadı') { currentRows=[];compareRows=[];currentData={};comparisonData={};for(const id of ['ptf-avg','smf-avg','yal-total','yat-total'])$(id).textContent='—';for(const id of ['ptf-range','smf-range'])$(id).textContent='Min — · Maks —';for(const id of ['ptf-delta','smf-delta','yal-delta','yat-delta']){$(id).textContent='';$(id).className='metric-delta neutral'}$('insight-text').textContent=message;renderTable();if(chart){chart.destroy();chart=null}if(quantityChart){quantityChart.destroy();quantityChart=null}setMarketEmptyState('price-chart',message);setMarketEmptyState('quantity-chart',message);setMarketEmptyState('direction-chart',message);const footer=$('piyasaFooterUpdated');if(footer)footer.textContent='—';closeHourDetail(); }
constrainMarketDate();
function setUser(user){$('user-email').textContent=user.name||user.email;$('user-initial').textContent=(user.name||user.email)[0].toUpperCase()}
function constrainMarketDate(announce=false){const input=$('date-input'),today=todayTR();input.max=today;let corrected=false;if(!input.value||input.value>today){input.value=today;corrected=true}$('next-date').disabled=input.value>=today;$('next-date').setAttribute('aria-disabled',String(input.value>=today));if(corrected&&announce)alertAt('data-alert','Bugünden ileri bir tarih seçilemez.');return corrected}
function shiftDate(days) { const input=$('date-input'),current=input.value||todayTR(),d=new Date(`${current}T12:00:00`); d.setDate(d.getDate()+days);const next=d.toISOString().slice(0,10),limited=next>todayTR()?todayTR():next;if(limited===current){constrainMarketDate();return}input.value=limited;constrainMarketDate();loadData(); }
function values(key){ return currentRows.map(r=>r[key]).filter(Number.isFinite); }
function rowValues(rows,key){return rows.map(r=>r[key]).filter(Number.isFinite)}
function aggregate(rows,key,mode='avg'){const v=rowValues(rows,key);if(!v.length)return null;if(mode==='total')return v.reduce((a,b)=>a+b,0);if(mode==='abs-total')return v.reduce((a,b)=>a+Math.abs(b),0);return v.reduce((a,b)=>a+b,0)/v.length}
function isoWeekParts(value){const date=new Date(`${value}T12:00:00Z`),weekday=date.getUTCDay()||7,thursday=new Date(date);thursday.setUTCDate(date.getUTCDate()+4-weekday);const year=thursday.getUTCFullYear(),yearStart=new Date(Date.UTC(year,0,1)),week=Math.ceil((((thursday-yearStart)/86400000)+1)/7);return{year,week,weekday}}
function isoWeeksInYear(year){return isoWeekParts(`${year}-12-28`).week}
function dateFromIsoWeek(year,week,weekday){const january4=new Date(Date.UTC(year,0,4)),january4Weekday=january4.getUTCDay()||7,monday=new Date(january4);monday.setUTCDate(january4.getUTCDate()-(january4Weekday-1));monday.setUTCDate(monday.getUTCDate()+(week-1)*7+(weekday-1));return monday.toISOString().slice(0,10)}
function previousYearSameWeekday(value){const parts=isoWeekParts(value),year=parts.year-1,week=Math.min(parts.week,isoWeeksInYear(year));return dateFromIsoWeek(year,week,parts.weekday)}
function shortDate(value){return value?value.split('-').reverse().join('.'):'—'}
function comparisonDate(){const mode=$('compare-select').value;if(mode==='none'){compareDateValue='';compareLabel='';return null}if(mode==='year-weekday'){compareDateValue=previousYearSameWeekday(currentDate);compareLabel=`${shortDate(compareDateValue)} · aynı hafta/gün`;return compareDateValue}const date=new Date(`${currentDate}T12:00:00`);date.setDate(date.getDate()-(mode==='week'?7:1));compareDateValue=date.toISOString().slice(0,10);compareLabel=mode==='week'?'7 gün önce':'Önceki gün';return compareDateValue}
function comparisonPhrase(){const mode=$('compare-select').value;return mode==='year-weekday'?'önceki yılın aynı hafta ve gününe':mode==='week'?'7 gün öncesine':'önceki güne'}
function comparisonBaseline(value,unit=''){return `Karşılaştırılan${compareDateValue?` (${shortDate(compareDateValue)})`:''}: ${fmt(value)}${unit?` ${unit}`:''}`}
function setDelta(key,target,mode='avg',unit=''){const current=aggregate(currentRows,key,mode),previous=aggregate(compareRows,key,mode),el=$(target);if(current==null||previous==null){el.textContent='';return}const baseline=comparisonBaseline(previous,unit);if(previous===0){el.innerHTML=`<span>Oran hesaplanamaz</span><small>${baseline}</small>`;el.className='metric-delta neutral';return}const percent=(current-previous)/previous*100;el.innerHTML=`<span>${percent>=0?'↑':'↓'} %${fmt(Math.abs(percent))} ${comparisonPhrase()} göre</span><small>${baseline}</small>`;el.className=`metric-delta ${percent>=0?'positive':'negative'}`}
function setDirectDelta(current,previous,target,unit=''){const el=$(target);if(current==null||previous==null){el.textContent='';return}const baseline=comparisonBaseline(previous,unit);if(previous===0){el.innerHTML=`<span>Oran hesaplanamaz</span><small>${baseline}</small>`;el.className='metric-delta neutral';return}const percent=(current-previous)/previous*100;el.innerHTML=`<span>${percent>=0?'↑':'↓'} %${fmt(Math.abs(percent))} ${comparisonPhrase()} göre</span><small>${baseline}</small>`;el.className=`metric-delta ${percent>=0?'positive':'negative'}`}
function summaryPrice(key, target, range) { const v=priceValues(currentRows,key); $(target).textContent=v.length?fmt(v.reduce((a,b)=>a+b,0)/v.length):'—'; if(range) $(range).textContent=v.length?`Min ${fmt(Math.min(...v))} · Maks ${fmt(Math.max(...v))}`:'Min — · Maks —'; }
function renderTable(loading=false) {
  $('data-body').innerHTML = loading ? Array.from({length:8},()=>`<tr>${'<td><div class="skeleton"></div></td>'.repeat(6)}</tr>`).join('') : currentRows.map(r=>{
    const direction=String(r.direction||'Veri yok'); const cls=/fazla|surplus|yukarı/i.test(direction)?'up':/açığ|deficit|aşağı/i.test(direction)?'down':'';
    return `<tr><td><b>${r.time}</b></td><td>${fmt(priceValue(r,'ptf'))}</td><td>${fmt(priceValue(r,'smf'))}</td><td>${fmt(r.yal)}</td><td>${fmt(r.yat)}</td><td><span class="direction ${cls}">${direction}</span></td></tr>`;
  }).join('') || '<tr><td colspan="6" class="text-center text-secondary py-5">Bu tarih için veri bulunamadı.</td></tr>';
}
function renderChart() {
  const foreignCurrency=currentCurrency!=='TRY';
  const series=[{name:'PTF',data:currentRows.map(r=>priceValue(r,'ptf'))}];
  if(!foreignCurrency)series.push({name:'SMF',data:currentRows.map(r=>priceValue(r,'smf'))});
  if(compareRows.length){series.push({name:`PTF · ${compareLabel}`,data:compareRows.map(r=>priceValue(r,'ptf'))});if(!foreignCurrency)series.push({name:`SMF · ${compareLabel}`,data:compareRows.map(r=>priceValue(r,'smf'))})}
  const options={chart:{type:'area',height:320,toolbar:{show:false},zoom:{enabled:false},selection:{enabled:false},fontFamily:SUITE_FONT,events:{click:(event,ctx,config)=>{if(config.dataPointIndex>=0)showHourDetail(config.dataPointIndex)},dataPointSelection:(event,ctx,config)=>showHourDetail(config.dataPointIndex)}},theme:{mode:document.documentElement.dataset.theme},series,colors:['#2c70f4','#8a67e8','#91b5fa','#b9a8ef'],stroke:{curve:'smooth',width:series.map((_,index)=>index<2?2:1.5),dashArray:series.map((_,index)=>compareRows.length&&index>=series.length/2?6:0)},markers:{size:0,hover:{size:5}},fill:{type:'gradient',gradient:{opacityFrom:.14,opacityTo:.01}},dataLabels:{enabled:false},xaxis:{categories:currentRows.map(r=>r.time),tickAmount:8,labels:{style:{colors:'#8994a6',fontSize:'10px'}}},yaxis:{labels:{formatter:v=>fmt(v),style:{colors:'#8994a6',fontSize:'10px'}}},grid:{borderColor:document.documentElement.dataset.theme==='dark'?'#293750':'#edf0f4',strokeDashArray:3},legend:{show:compareRows.length>0,position:'bottom',fontSize:'11px'},tooltip:{shared:true,intersect:false,y:{formatter:v=>`${fmt(v)} ${currencyUnit()}`}},noData:{text:'Veri bulunamadı'}};
  if(chart) chart.destroy(); chart=new ApexCharts($('price-chart'),options); chart.render();
}
function renderNextDayPtf(data={}){
  nextDayPtfData=data||{};const rows=nextDayPtfData.rows||[],available=nextDayPtfData.currencyInfo?.available||['TRY'],displayCurrency=available.includes(currentCurrency)?currentCurrency:'TRY',nextPrice=row=>{const value=row?.ptfByCurrency?.[displayCurrency];return Number.isFinite(value)?value:(displayCurrency==='TRY'&&Number.isFinite(row?.ptf)?row.ptf:null)},displayUnit=CURRENCIES[displayCurrency]?.unit||CURRENCIES.TRY.unit,pricedRows=rows.filter(row=>Number.isFinite(nextPrice(row))),status=$('next-day-ptf-status'),publicationStatus=nextDayPtfData.publication?.status||'final';
  $('next-day-ptf-date').textContent=marketDateLabel(nextDayPtfData.date);
  $('next-day-ptf-unit').textContent=displayUnit;
  $('next-day-ptf-hours').textContent=String(nextDayPtfData.summary?.publishedHours??0);
  if(nextDayPtfChart){nextDayPtfChart.destroy();nextDayPtfChart=null}
  if(!pricedRows.length){
    $('next-day-ptf-average').textContent='—';$('next-day-ptf-min').textContent='—';$('next-day-ptf-max').textContent='—';$('next-day-ptf-min-hour').textContent='—';$('next-day-ptf-max-hour').textContent='—';
    status.className=nextDayPtfData.published?'currency-missing':'waiting';status.innerHTML=`<i></i> ${nextDayPtfData.published?`${CURRENCIES[displayCurrency].label} verisi yok`:(nextDayPtfData.publication?.label||'Henüz yayımlanmadı')}`;
    $('next-day-ptf-chart').innerHTML='<div class="next-day-ptf-empty">EPİAŞ ertesi gün PTF değerlerini yayımladığında bu alan otomatik olarak dolacak.</div>';return;
  }
  const average=nextDayPtfData.summary?.ptfAverageByCurrency?.[displayCurrency]??(pricedRows.reduce((sum,row)=>sum+nextPrice(row),0)/pricedRows.length);
  const minimum=pricedRows.reduce((best,row)=>!best||nextPrice(row)<nextPrice(best)?row:best,null),maximum=pricedRows.reduce((best,row)=>!best||nextPrice(row)>nextPrice(best)?row:best,null);
  $('next-day-ptf-average').textContent=fmt(average);$('next-day-ptf-min').textContent=fmt(nextPrice(minimum));$('next-day-ptf-max').textContent=fmt(nextPrice(maximum));$('next-day-ptf-min-hour').textContent=minimum?.time||'—';$('next-day-ptf-max-hour').textContent=maximum?.time||'—';
  const effectiveStatus=publicationStatus==='final'?'final':'preliminary';status.className=effectiveStatus==='final'?'published':'preliminary';status.innerHTML=`<i></i> ${effectiveStatus==='final'?'Kesinleşmiş PTF':'Kesinleşmemiş PTF'}`;
  const options={chart:{type:'area',height:270,toolbar:{show:false},zoom:{enabled:false},selection:{enabled:false},fontFamily:SUITE_FONT},theme:{mode:document.documentElement.dataset.theme},series:[{name:`PTF · ${CURRENCIES[displayCurrency].label}`,data:rows.map(nextPrice)}],colors:['#2d70ee'],stroke:{curve:'smooth',width:2.5},markers:{size:0,hover:{size:5}},fill:{type:'gradient',gradient:{opacityFrom:.18,opacityTo:.02}},dataLabels:{enabled:false},xaxis:{categories:rows.map(row=>row.time),tickAmount:8,labels:{style:{colors:'#8994a6',fontSize:'10px'}}},yaxis:{labels:{formatter:value=>fmt(value),style:{colors:'#8994a6',fontSize:'10px'}}},grid:{borderColor:document.documentElement.dataset.theme==='dark'?'#293750':'#edf0f4',strokeDashArray:3},legend:{show:false},tooltip:{shared:true,intersect:false,y:{formatter:value=>`${fmt(value)} ${displayUnit}`}},noData:{text:'PTF verisi bulunamadı'}};
  $('next-day-ptf-chart').innerHTML='';nextDayPtfChart=new ApexCharts($('next-day-ptf-chart'),options);nextDayPtfChart.render();
}
function scheduleNextDayPtfRefresh(data,baseDate,sequence){clearTimeout(nextDayPtfRefreshTimer);nextDayPtfRefreshTimer=0;const refreshAt=data?.publication?.nextRefreshAt;if(!refreshAt)return;const delay=new Date(refreshAt).getTime()-Date.now()+1500;if(!Number.isFinite(delay)||delay<=0||delay>86400000)return;nextDayPtfRefreshTimer=window.setTimeout(()=>{if(sequence===loadSequence)loadNextDayPtf(baseDate,true,sequence)},delay)}
async function loadNextDayPtf(baseDate,force=false,sequence=loadSequence){
  clearTimeout(nextDayPtfRefreshTimer);nextDayPtfRefreshTimer=0;const status=$('next-day-ptf-status');status.className='loading';status.innerHTML='<i></i> Yükleniyor';setMarketConnectionPart('next','loading','Ertesi gün PTF alınıyor');
  try{const refreshQuery=force?'&refresh=1':'';const data=await api(`/api/next-day-ptf?date=${encodeURIComponent(baseDate)}${refreshQuery}`);if(sequence!==loadSequence)return;renderNextDayPtf(data);scheduleNextDayPtfRefresh(data,baseDate,sequence);setMarketConnectionPart('next','live','Ertesi gün PTF servisi doğrulandı')}
  catch(error){if(sequence!==loadSequence)return;nextDayPtfData={};if(nextDayPtfChart){nextDayPtfChart.destroy();nextDayPtfChart=null}status.className='error';status.innerHTML='<i></i> Alınamadı';$('next-day-ptf-date').textContent='—';$('next-day-ptf-chart').innerHTML=`<div class="next-day-ptf-empty">${error.message||'Ertesi gün PTF verisine ulaşılamadı.'}</div>`;setMarketConnectionPart('next','warning',error.message||'Ertesi gün PTF verisine ulaşılamadı')}
}
function renderInsight(){if(!currentRows.length){$('insight-text').textContent='Bu tarih için yorumlanabilecek veri bulunamadı.';return}const ptfRows=currentRows.filter(r=>Number.isFinite(priceValue(r,'ptf')));const peak=ptfRows.reduce((best,row)=>!best||priceValue(row,'ptf')>priceValue(best,'ptf')?row:best,null);const low=ptfRows.reduce((best,row)=>!best||priceValue(row,'ptf')<priceValue(best,'ptf')?row:best,null);const types=currentRows.map(r=>directionType(r.direction));const deficit=types.filter(v=>v==='deficit').length;const surplus=types.filter(v=>v==='surplus').length;const direction=deficit>surplus?`${deficit} saat enerji açığı`:surplus>deficit?`${surplus} saat enerji fazlası`:'dengeli bir sistem yönü';const commonRows=currentRows.filter(row=>Number.isFinite(row.ptf)&&Number.isFinite(row.smf));const commonHours=Number(currentData?.summary?.ptfSmfCommonHours??commonRows.length);const commonDifference=currentData?.summary?.smfPtfAverageDifference??(commonRows.length?commonRows.reduce((sum,row)=>sum+(row.smf-row.ptf),0)/commonRows.length:null);const comparison=currentCurrency==='TRY'?(Number.isFinite(commonDifference)?`SMF–PTF ortalama farkı yalnızca ${commonHours} ortak saatte ${fmt(commonDifference)} TL/MWh;`:'PTF–SMF için ortak yayımlanmış saat bulunmadı;'):'Döviz seçimi yalnızca EPİAŞ PTF değerine uygulanıyor; SMF TL/MWh olarak ayrı gösteriliyor.';$('insight-text').textContent=`PTF en yüksek ${peak?.time||'—'} saatinde ${fmt(priceValue(peak,'ptf'))} ${currencyUnit()}, en düşük ${low?.time||'—'} saatinde ${fmt(priceValue(low,'ptf'))} ${currencyUnit()} oldu. ${comparison} Gün genelinde ${direction} gözlendi.`}
function directionType(value){const text=String(value||'').toLocaleLowerCase('tr-TR');if(text.includes('açığ')||text.includes('deficit'))return'deficit';if(text.includes('fazla')||text.includes('surplus'))return'surplus';if(text.includes('denge')||text.includes('balanced'))return'balanced';return'missing'}
function showValidation(data){const checks=data.validation||{};const details=Object.entries(checks).map(([key,value])=>`${key.toUpperCase()}: ${value.items??0} kayıt · ${value.field||''}`).join('\n');const warnings=data.warnings||[];const incomplete=warnings.length||!data.rows?.length;setMarketConnectionPart('main',incomplete?'warning':'live',[...warnings,details].filter(Boolean).join('\n'))}
function renderDirectionTimeline(){const items=currentRows.map(row=>{const type=directionType(row.direction);const label=type==='deficit'?'Enerji Açığı':type==='surplus'?'Enerji Fazlası':type==='balanced'?'Dengede':'Veri Yok';return{...row,type,label}});const counts={deficit:0,surplus:0,balanced:0,missing:0};items.forEach(item=>counts[item.type]++);$('direction-chart').innerHTML=`<div class="direction-summary"><span class="deficit"><i></i><b>${counts.deficit}</b> saat enerji açığı</span><span class="surplus"><i></i><b>${counts.surplus}</b> saat enerji fazlası</span><span class="balanced"><i></i><b>${counts.balanced}</b> saat dengede</span>${counts.missing?`<span class="missing"><i></i><b>${counts.missing}</b> saat veri yok</span>`:''}</div><div class="direction-hours">${items.map(item=>`<div class="direction-hour ${item.type}" title="${item.time} · ${item.label}"><strong>${item.time.slice(0,2)}</strong><span>${item.label}</span></div>`).join('')}</div><div class="direction-scale"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>23:00</span></div>`}
function renderOperationalCharts(){const common={chart:{height:250,toolbar:{show:false},zoom:{enabled:false},selection:{enabled:false},fontFamily:SUITE_FONT},dataLabels:{enabled:false},grid:{borderColor:'#edf0f4',strokeDashArray:3},xaxis:{categories:currentRows.map(r=>r.time),tickAmount:6,labels:{style:{colors:'#8994a6',fontSize:'9px'}}},legend:{position:'bottom',fontSize:'11px'}};if(quantityChart)quantityChart.destroy();quantityChart=new ApexCharts($('quantity-chart'),{...common,chart:{...common.chart,type:'bar',stacked:false},series:[{name:'YAL',data:currentRows.map(r=>r.yal)},{name:'YAT',data:currentRows.map(r=>Math.abs(r.yat||0))}],colors:['#30b879','#f49b3f'],plotOptions:{bar:{columnWidth:'62%',borderRadius:2}},yaxis:{labels:{formatter:v=>fmt(v),style:{colors:'#8994a6',fontSize:'9px'}}},tooltip:{shared:true,intersect:false,y:{formatter:v=>`${fmt(v)} MWh`}}});quantityChart.render();renderDirectionTimeline()}
function updateCurrencyUi(info={}){const available=info.available||['TRY'];if(!available.includes(currentCurrency))currentCurrency='TRY';const config=CURRENCIES[currentCurrency]||CURRENCIES.TRY;const tryConfig=CURRENCIES.TRY;document.querySelectorAll('[data-currency]').forEach(button=>{const selected=button.dataset.currency===currentCurrency;button.classList.toggle('active',selected);button.setAttribute('aria-pressed',String(selected));button.disabled=!available.includes(button.dataset.currency)});for(const id of ['ptf-unit','table-ptf-unit','detail-ptf-unit'])$(id).textContent=config.unit;for(const id of ['smf-unit','table-smf-unit','detail-smf-unit'])$(id).textContent=tryConfig.unit;$('ptf-currency-icon').textContent=config.icon;$('smf-currency-icon').textContent=tryConfig.icon;$('price-chart-title').textContent=currentCurrency==='TRY'?'PTF & SMF Fiyat Eğrisi':`PTF Fiyat Eğrisi · ${config.label}`;$('smf-chart-key').classList.toggle('d-none',currentCurrency!=='TRY');$('currency-note').textContent=currentCurrency==='TRY'?'PTF ve SMF, TL/MWh olarak gösteriliyor.':'Döviz seçimi yalnızca PTF’ye uygulanır. PTF değeri EPİAŞ’ın doğrudan priceEur / priceUsd alanından alınır; SMF TL/MWh olarak kalır.'}
function renderDashboard(data,comparison={rows:[]}){currentData=data||{};comparisonData=comparison||{rows:[]};compareRows=comparisonData.rows||[];updateCurrencyUi(data.currencyInfo);summaryPrice('ptf','ptf-avg','ptf-range');summaryPrice('smf','smf-avg','smf-range');const currentPtf=priceSummary(data,'ptf',currentRows);const currentSmf=priceSummary(data,'smf',currentRows);const comparePtf=priceSummary(comparisonData,'ptf',compareRows);const compareSmf=priceSummary(comparisonData,'smf',compareRows);const currentYal=data.summary?.yalTotal??aggregate(currentRows,'yal','total');const currentYat=data.summary?.yatTotal??aggregate(currentRows,'yat','abs-total');const compareYal=comparisonData.summary?.yalTotal??aggregate(compareRows,'yal','total');const compareYat=comparisonData.summary?.yatTotal??aggregate(compareRows,'yat','abs-total');if(currentPtf!=null)$('ptf-avg').textContent=fmt(currentPtf);if(currentSmf!=null)$('smf-avg').textContent=fmt(currentSmf);$('yal-total').textContent=currentYal!=null?fmt(currentYal):'—';$('yat-total').textContent=currentYat!=null?fmt(currentYat):'—';setDirectDelta(currentPtf,comparePtf,'ptf-delta',currencyUnit());setDirectDelta(currentSmf,compareSmf,'smf-delta',priceUnit('smf'));setDirectDelta(currentYal,compareYal,'yal-delta','MWh');setDirectDelta(currentYat,compareYat,'yat-delta','MWh');renderTable();renderChart();renderInsight();renderOperationalCharts();showValidation(data);if(data.warnings?.length)alertAt('data-alert',data.warnings.join(' '));const updatedDate=new Date(data.updatedAt);$('last-update').textContent=`Son güncelleme ${updatedDate.toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit'})}${data.cached?' · Önbellekten':''}`;const footerUpdated=$('piyasaFooterUpdated');if(footerUpdated)footerUpdated.textContent=updatedDate.toLocaleString('tr-TR',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});window.BahaTracking?.publish({module:'piyasa',date:currentDate,ptfAverage:data.summary?.ptfAverageByCurrency?.TRY??data.summary?.ptfAverage??aggregate(currentRows,'ptf','avg'),smfAverage:data.summary?.smfAverage})}
let loadSequence=0;
async function loadData(force=false){
  constrainMarketDate();
  const sequence=++loadSequence;
  currentDate=$('date-input').value;
  alertAt('data-alert');
  $('refresh-button').disabled=true;
  setMarketConnectionPart('main','loading','Piyasa verileri alınıyor');
  loadNextDayPtf(currentDate,force,sequence);
  const compareDate=comparisonDate();
  let data=!force?readDataCache(currentDate):null;
  const fromCache=Boolean(data);
  if(!data)renderTable(true);
  try{
    if(!data){
      const refreshQuery=force?'&refresh=1':'';
      data=await api(`/api/data?date=${encodeURIComponent(currentDate)}${refreshQuery}`);
      writeDataCache(currentDate,data);
    }
    if(sequence!==loadSequence)return;
    currentRows=data.rows||[];
    const cachedComparison=compareDate&&!force?readDataCache(compareDate):null;
    renderDashboard(data,cachedComparison||{rows:[]});
    if(fromCache)$('last-update').textContent+=' · Cihaz önbelleğinden';
    if(compareDate&&!cachedComparison){
      $('last-update').textContent+=' · Karşılaştırma yükleniyor…';
      let comparison;
      try{
        comparison=await api(`/api/data?date=${encodeURIComponent(compareDate)}`);
      }catch(error){
        if(error.status===401)throw error;
        if(sequence!==loadSequence)return;
        comparison={rows:[]};
        renderDashboard(data,comparison);
        const message='Ana piyasa verisi gösteriliyor; karşılaştırma verisi alınamadı.';
        setMarketConnectionPart('main','warning',message);
        alertAt('data-alert',message);
        return;
      }
      if(sequence!==loadSequence)return;
      if(comparison.rows?.length)writeDataCache(compareDate,comparison);
      renderDashboard(data,comparison);
      if(!comparison.rows?.length){
        const message='Ana piyasa verisi gösteriliyor; seçilen karşılaştırma dönemi için veri yok.';
        setMarketConnectionPart('main','warning',message);
        alertAt('data-alert',message);
      }
    }
  }
  catch(e){
    if(sequence!==loadSequence)return;
    const message=e.message||'Piyasa verileri alınamadı.';
    clearMarketDashboard(message);
    setMarketConnectionPart('main','error',message);
    alertAt('data-alert',message);
    if(e.status===401){show('login');alertAt('login-alert','Oturum süreniz doldu. Lütfen yeniden giriş yapın.')}
  }finally{
    if(sequence===loadSequence)$('refresh-button').disabled=false;
  }
}
$('login-form').addEventListener('submit',async e=>{e.preventDefault();alertAt('login-alert');$('login-button').disabled=true;try{const payload={email:$('email').value,password:$('password').value};const data=await api('/api/login',{method:'POST',body:JSON.stringify(payload)});$('password').value='';setUser(data);show('app');$('date-input').value=todayTR();loadData()}catch(err){alertAt('login-alert',err.message)}finally{$('login-button').disabled=false}});
$('password-toggle').addEventListener('click',()=>{const visible=$('password').type==='text';$('password').type=visible?'password':'text';$('password-toggle').setAttribute('aria-label',visible?'Şifreyi göster':'Şifreyi gizle');$('password-toggle').setAttribute('aria-pressed',String(!visible));document.querySelector('.eye-open').classList.toggle('d-none',!visible);document.querySelector('.eye-closed').classList.toggle('d-none',visible);$('password').focus()});
$('logout').onclick=async()=>{await api('/api/logout',{method:'POST'}).catch(()=>{});if(!desktopSidebar.matches)closeMenu();window.location.replace('/oturum-kapatildi')};$('refresh-button').onclick=()=>loadData(true);$('date-input').onchange=()=>{constrainMarketDate(true);loadData()};$('prev-date').onclick=()=>shiftDate(-1);$('next-date').onclick=()=>shiftDate(1);$('today-button').onclick=()=>{$('date-input').value=todayTR();constrainMarketDate();loadData()};
const sidebar=document.querySelector('.sidebar');
const desktopSidebar=window.matchMedia('(min-width:821px)'),sidebarStorageKey='baha-sidebar-collapsed';
let desktopSidebarHoverTimer=0,desktopSidebarPointerInside=false;
function setMenu(open,persist=true){if(desktopSidebar.matches){const collapsed=!open;sidebar.classList.remove('open');document.body.classList.remove('sidebar-open','suite-sidebar-hovered');document.body.classList.toggle('suite-sidebar-collapsed',collapsed);$('menu-button').setAttribute('aria-expanded',String(open));if(persist)localStorage.setItem(sidebarStorageKey,String(collapsed));return}document.body.classList.remove('suite-sidebar-collapsed','suite-sidebar-hovered');sidebar.classList.toggle('open',open);document.body.classList.toggle('sidebar-open',open);$('menu-button').setAttribute('aria-expanded',String(open))}
function setDesktopSidebarHover(open){if(!desktopSidebar.matches||!document.body.classList.contains('suite-sidebar-collapsed'))return;window.clearTimeout(desktopSidebarHoverTimer);const apply=()=>{document.body.classList.toggle('suite-sidebar-hovered',open);$('menu-button').setAttribute('aria-expanded',String(open))};if(open)apply();else desktopSidebarHoverTimer=window.setTimeout(apply,120)}
function closeMenu(){setDesktopSidebarHover(false);setMenu(false)}
function syncMenuMode(){if(desktopSidebar.matches)setMenu(false,false);else setMenu(false,false)}
sidebar.addEventListener('mouseenter',()=>{desktopSidebarPointerInside=true;setDesktopSidebarHover(true)});
sidebar.addEventListener('mouseleave',()=>{desktopSidebarPointerInside=false;setDesktopSidebarHover(false)});
sidebar.addEventListener('focusin',()=>setDesktopSidebarHover(true));
sidebar.addEventListener('focusout',event=>{if(!desktopSidebarPointerInside&&!sidebar.contains(event.relatedTarget))setDesktopSidebarHover(false)});
$('menu-button').onclick=()=>setMenu(!sidebar.classList.contains('open'));
$('menu-close').onclick=closeMenu;
$('sidebar-overlay').onclick=closeMenu;
document.addEventListener('keydown',event=>{if(event.key!=='Escape')return;if(desktopSidebar.matches)setDesktopSidebarHover(false);else closeMenu()});
desktopSidebar.addEventListener('change',syncMenuMode);syncMenuMode();
$('compare-select').onchange=()=>loadData();
document.querySelectorAll('[data-currency]').forEach(button=>button.addEventListener('click',()=>{if(button.disabled||button.dataset.currency===currentCurrency)return;currentCurrency=button.dataset.currency;localStorage.setItem('baha-market-currency',currentCurrency);if(currentRows.length)renderDashboard(currentData,comparisonData);else updateCurrencyUi(currentData.currencyInfo);if(nextDayPtfData.date)renderNextDayPtf(nextDayPtfData)}));
$('theme-toggle').onclick=()=>{const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';localStorage.setItem('baha-theme',theme);applyTheme(theme);if(currentRows.length){renderChart();renderOperationalCharts()}if(nextDayPtfData.date)renderNextDayPtf(nextDayPtfData)};
applyTheme(selectedTheme());
systemDark.addEventListener('change',()=>{if(!localStorage.getItem('baha-theme')){applyTheme(selectedTheme());if(currentRows.length){renderChart();renderOperationalCharts()}if(nextDayPtfData.date)renderNextDayPtf(nextDayPtfData)}});
$('hour-detail-close').onclick=closeHourDetail;
$('hour-detail-backdrop').onclick=closeHourDetail;
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeHourDetail()});

let pullStart=0,pullDistance=0,pulling=false;
const pullRefresh=$('pull-refresh');
document.addEventListener('touchstart',event=>{if(window.scrollY>0||document.body.classList.contains('auth-open')||event.target.closest('button,input,select,.apexcharts-canvas,.sidebar'))return;pullStart=event.touches[0].clientY;pulling=true},{passive:true});
document.addEventListener('touchmove',event=>{if(!pulling)return;pullDistance=Math.max(0,Math.min(110,(event.touches[0].clientY-pullStart)*.55));if(!pullDistance)return;event.preventDefault();pullRefresh.style.transform=`translate(-50%,${pullDistance}px)`;pullRefresh.classList.toggle('ready',pullDistance>=70);pullRefresh.querySelector('small').textContent=pullDistance>=70?'Yenilemek için bırakın':'Yenilemek için çekin'},{passive:false});
document.addEventListener('touchend',async()=>{if(!pulling)return;pulling=false;const refresh=pullDistance>=70;pullDistance=0;if(refresh){pullRefresh.classList.add('loading');pullRefresh.querySelector('small').textContent='Veriler yenileniyor…';await loadData(true)}pullRefresh.classList.remove('ready','loading');pullRefresh.style.transform='';pullRefresh.querySelector('small').textContent='Yenilemek için çekin'},{passive:true});
document.querySelectorAll('.sidebar nav a').forEach(link=>link.addEventListener('click',()=>{document.querySelectorAll('.sidebar nav a').forEach(item=>item.classList.remove('active'));link.classList.add('active');if(!desktopSidebar.matches)closeMenu()}));

let installPrompt;
const installButton = $('install-app');
const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone;
if ('serviceWorker' in navigator) window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
window.addEventListener('beforeinstallprompt', event => {
  event.preventDefault(); installPrompt = event; installButton.classList.remove('d-none');
});
installButton.addEventListener('click', async () => {
  if (!installPrompt) return;
  installPrompt.prompt(); await installPrompt.userChoice; installPrompt = null; installButton.classList.add('d-none');
});
window.addEventListener('appinstalled', () => installButton.classList.add('d-none'));
if (/iphone|ipad|ipod/i.test(navigator.userAgent) && !standalone) $('ios-install-hint').classList.remove('d-none');
$('xlsx-button').onclick=()=>{if(!currentRows.length)return;if(!window.XLSX){alertAt('data-alert','Excel oluşturma bileşeni yüklenemedi. İnternet bağlantınızı kontrol edin.');return}const rows=[['Tarih','Saat','PTF (TL/MWh)','SMF (TL/MWh)','YAL (MWh)','YAT (MWh)','Sistem Yönü'],...currentRows.map(r=>[currentDate,r.time,r.ptf,r.smf,r.yal,Number.isFinite(r.yat)?Math.abs(r.yat):null,r.direction||''])];const sheet=XLSX.utils.aoa_to_sheet(rows);sheet['!cols']=[{wch:13},{wch:9},{wch:16},{wch:16},{wch:14},{wch:14},{wch:22}];for(let row=2;row<=rows.length;row++)for(const column of ['C','D','E','F'])if(sheet[`${column}${row}`])sheet[`${column}${row}`].z='#,##0.00';sheet['!autofilter']={ref:`A1:G${rows.length}`};const book=XLSX.utils.book_new();XLSX.utils.book_append_sheet(book,sheet,'Saatlik Veriler');XLSX.writeFile(book,`baha-enerji-${currentDate}.xlsx`,{compression:true})};
(async()=>{try{const s=await api('/api/session');setUser(s);show('app');$('date-input').value=todayTR();constrainMarketDate();loadData()}catch{show('login')}})();
