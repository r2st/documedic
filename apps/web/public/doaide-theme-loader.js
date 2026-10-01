(function(){
  if(!document.querySelector('link[href*="doaide-theme"]')){
    var l=document.createElement('link');
    l.rel='stylesheet';
    l.href='https://doaide.com/doaide-theme.css';
    l.crossOrigin='anonymous';
    document.head.appendChild(l);
  }
})();
