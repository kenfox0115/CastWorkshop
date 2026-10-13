// Preserve native text as the accessible name, including dynamically rendered media controls.
(() => {
  const selector = "#settings-open,#debug-open,#character-new,#character-open,.dialog-head button:not(#job-cancel-form button),#image-view-close,.tools [data-mode=\"chat\"],.tools [data-mode=\"image\"],.tools [data-mode=\"video\"],#composer button,#multi-image-open,#multi-video-open,#job-retry,#diagnostics-refresh,.reroll,.download-media,#character-delete,#multi-video-back,#video-chain-previous,#video-chain-next,#video-chain-fullscreen,#scene-open,#scene-new,#scene-anchor-generate,#multi-video-confirm,#image-anchor-approve,#diagnostics-copy,.play-video-chain,.continue-video";
  function label(control) {
    const text = control.textContent.trim();
    if (text) control.title = text === '+ New' ? 'New character' : text;
    if (control.matches('.tools button')) control.setAttribute('aria-pressed', String(control.classList.contains('active')));
  }
  function scan(root) {
    if (root.nodeType !== 1 && root !== document) return;
    if (root.matches?.(selector)) label(root);
    root.querySelectorAll(selector).forEach(label);
  }
  scan(document);
  new MutationObserver(records => {
    for (const record of records) {
      if (record.type === 'attributes') { if (record.target.matches(selector)) label(record.target); continue; }
      const control = record.target.closest?.(selector) || record.target.parentElement?.closest(selector);
      if (control) label(control);
      record.addedNodes.forEach(scan);
    }
  }).observe(document.body, {subtree:true, childList:true, characterData:true, attributes:true, attributeFilter:['class']});
})();
