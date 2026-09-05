function s(o,e,n){const r=[];let i=-1;for(;++i<o.length;){const l=o[i].resolveAll;l&&!r.includes(l)&&(e=l(e,n),r.push(l))}return e}export{s as r};
