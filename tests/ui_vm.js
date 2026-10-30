/* Headless report-controller fixture. Full SDK DOM/navigation is tested in Playwright. */
module.exports.install=(context,vm)=>{
 vm.createContext(context);
 for(const [,id] of require('node:fs').readFileSync(process.argv[1],'utf8').matchAll(/\bid="([^"]+)"/g)){
  const node=context.document.querySelector('#'+id);node.innerHTML??='';node.textContent??='';
 }

 context.getComputedStyle=()=>({getPropertyValue:()=> '#222222'});
 context.ErgUI={
  mount(){},save(){},money(){},projects(){},work(){},accounts(){},session(){},metrics(){},overviewProjects(){},
  view(key){if(key==='costs')vm.runInContext("loadEffectiveness(false,'work')",context)},
  initialize(){vm.runInContext("showView(location.hash.slice(1)||'usage')",context)},
  openFromProject(row){context.__row=row;vm.runInContext("showView('history');openHistory(__row)",context)},
 };
};
