const input=document.getElementById('filter');
const viewButtons=[...document.querySelectorAll('.view-button')];
function showView(name){
  if(!viewButtons.some(button=>button.dataset.view===name))name='datasets';
  sessionStorage.setItem('hawtown-view',name);
  document.querySelectorAll('main > .view').forEach(view=>view.hidden=view.id!=='view-'+name);
  viewButtons.forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.view===name)));
}
viewButtons.forEach(button=>button.addEventListener('click',()=>showView(button.dataset.view)));
showView(sessionStorage.getItem('hawtown-view')||'datasets');
document.querySelectorAll('.jump').forEach(button=>button.addEventListener('click',()=>{
  showView('runs');
  input.value=[button.dataset.dataset,button.dataset.ligand,button.dataset.runKind].filter(Boolean).join(' ');
  input.dispatchEvent(new Event('input'));input.focus();
}));
const compoundInput=document.getElementById('compound-filter');
compoundInput.addEventListener('input',()=>{
  sessionStorage.setItem('hawtown-compound-filter',compoundInput.value);
  const rows=[...document.querySelectorAll('.compound-table tbody tr')];
  rows.forEach(row=>row.hidden=!row.dataset.search.includes(compoundInput.value.trim().toLowerCase()));
  document.getElementById('compound-count').textContent=rows.filter(row=>!row.hidden).length+' / '+rows.length+' compounds';
});

input.addEventListener('input',()=>{sessionStorage.setItem('hawtown-run-filter',input.value);const q=input.value.trim().toLowerCase();let visible=0;document.querySelectorAll('.run-table tbody tr').forEach(row=>{row.hidden=!row.dataset.search.includes(q);if(!row.hidden)visible++;});document.getElementById('row-count').textContent=visible+' / '+document.querySelectorAll('.run-table tbody tr').length+' records';});
const tbody=document.querySelector('.run-table tbody');
const sortButtons=[...document.querySelectorAll('button.sort')];
const collator=new Intl.Collator(undefined,{numeric:true,sensitivity:'base'});
sortButtons.forEach(button=>button.addEventListener('click',()=>{
  const header=button.closest('th');
  const ascending=header.getAttribute('aria-sort')!=='ascending';
  const column=Number(button.dataset.column);
  sessionStorage.setItem('hawtown-sort',JSON.stringify({column,ascending}));
  const numeric=[5,7,8].includes(column);
  const value=row=>row.cells[column].textContent.trim();
  const missing=text=>['','?','unknown','n/a'].includes(text.toLowerCase());
  const rows=[...tbody.rows];
  rows.sort((a,b)=>{
    const left=value(a),right=value(b);
    if(missing(left)||missing(right))return Number(missing(left))-Number(missing(right));
    const result=numeric?Number(left)-Number(right):collator.compare(left,right);
    return ascending?result:-result;
  });
  rows.forEach(row=>tbody.appendChild(row));
  sortButtons.forEach(other=>{
    const active=other===button;
    other.closest('th').setAttribute('aria-sort',active?(ascending?'ascending':'descending'):'none');
    other.querySelector('span').textContent=active?(ascending?'↑':'↓'):'↕';
    other.setAttribute('aria-label','Sort by '+other.childNodes[0].textContent+', '+(active&&ascending?'descending':'ascending'));
  });
}));
input.value=sessionStorage.getItem('hawtown-run-filter')||'';input.dispatchEvent(new Event('input'));
compoundInput.value=sessionStorage.getItem('hawtown-compound-filter')||'';compoundInput.dispatchEvent(new Event('input'));
try{const saved=JSON.parse(sessionStorage.getItem('hawtown-sort')||'null');if(saved){const button=sortButtons.find(item=>Number(item.dataset.column)===saved.column);if(button){button.click();if(!saved.ascending)button.click();}}}catch{sessionStorage.removeItem('hawtown-sort');}
if(location.hostname==='127.0.0.1'&&location.port==='8765')setInterval(()=>{if(!document.querySelector('details.run[open]')&&document.activeElement.tagName!=='INPUT')location.reload();},300000);
