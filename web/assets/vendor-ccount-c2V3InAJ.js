function r(i,e){const t=String(i);if(typeof e!="string")throw new TypeError("Expected character");let o=0,n=t.indexOf(e);for(;n!==-1;)o++,n=t.indexOf(e,n+e.length);return o}export{r as c};
