import {createRequire} from 'node:module';
import {readFileSync,writeFileSync,copyFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const sdk=process.env.ERG_UX_BUILDER_ROOT;
if(!sdk)throw Error('Set ERG_UX_BUILDER_ROOT to the UX Builder checkout');
for(const [name,version] of [['ui','0.14.0'],['tokens','1.7.0']]){const actual=JSON.parse(readFileSync(sdk+'/packages/'+name+'/package.json')).version;if(actual!==version)throw Error('Expected @ux-builder/'+name+' '+version+', found '+actual)}
const root=process.argv[2]||process.cwd();
const require=createRequire(sdk+'/package.json');
const {createElement:h}=await import(pathToFileURL(require.resolve('react')));
const {renderToStaticMarkup:render}=await import(pathToFileURL(require.resolve('react-dom/server')));
const P=await import(pathToFileURL(sdk+'/packages/ui/dist/patterns.js'));
const ui=await import(pathToFileURL(sdk+'/packages/ui/dist/index.js'));
const nav=[['overview','Overview'],['projects','Projects'],['sessions','Sessions'],['accounts','Accounts']].map(([id,label])=>({id,label,href:'#'+id}));
const shells=Object.fromEntries(['side-nav','top-nav'].map(frame=>[frame,render(h(P.AppShell,{nav,frame,current:'overview',brand:h('strong',null,'erg'),actions:h(ui.Button,{'data-destination':'system',variant:'text'},'System')},h(P.Page,null,h('div',{id:'erg-content'}))))]));
writeFileSync(root+'/erg/static/shell.js','/* Generated from @ux-builder/ui 0.14.0; scripts/build-ui.mjs. */\nwindow.ergShells='+JSON.stringify(shells)+';\n');
for(const name of ['ui.css','patterns.css'])copyFileSync(sdk+'/packages/ui/dist/'+name,root+'/erg/static/'+name);
copyFileSync(sdk+'/packages/tokens/dist/themes/synaptiq.css',root+'/erg/static/theme.css');
writeFileSync(root+'/erg/static/UX-BUILDER-LICENSE.txt',readFileSync(sdk+'/LICENSE-MIT','utf8')+'\n'+readFileSync(sdk+'/LICENSE-APACHE','utf8'));
console.log('Generated SDK AppShell frames and pinned CSS assets');
