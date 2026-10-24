// Site adapter only: no external API calls, eval, login, voting, or pagination crawling.
(maxComments) => {
  const post = document.querySelector('.nsk-post .content-item[id="0"], .content-item[id="0"], .nsk-post');
  const body = post?.querySelector('article.post-content') || document.querySelector('article.post-content');
  if (!body) return {error: 'missing_post'};
  const permitted = new Set(['P','DIV','SPAN','BR','HR','H1','H2','H3','H4','H5','H6',
    'STRONG','B','EM','I','S','DEL','U','SUB','SUP','BLOCKQUOTE','PRE','CODE',
    'UL','OL','LI','TABLE','THEAD','TBODY','TR','TH','TD','A','IMG','DETAILS','SUMMARY']);
  const clean = (source) => {
    const root = document.createElement('div');
    const visit = (node, parent) => {
      if (node.nodeType === Node.TEXT_NODE) {
        parent.appendChild(document.createTextNode(node.textContent));
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      if (['SCRIPT','STYLE','IFRAME','OBJECT','EMBED','FORM','INPUT','BUTTON','TEXTAREA','SVG','NOSCRIPT'].includes(node.tagName)) return;
      if (!permitted.has(node.tagName)) {
        for (const child of node.childNodes) visit(child, parent);
        return;
      }
      const copy = document.createElement(node.tagName.toLowerCase());
      for (const attr of ['alt','title','colspan','rowspan']) {
        if (node.hasAttribute(attr)) copy.setAttribute(attr, node.getAttribute(attr).slice(0, 300));
      }
      for (const attr of ['href','src']) {
        let raw = node.getAttribute(attr);
        if (attr === 'src' && !raw) raw = node.getAttribute('data-src');
        if (!raw) continue;
        try {
          const url = new URL(raw, document.baseURI);
          if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) copy.setAttribute(attr, url.href);
        } catch (_) { /* Invalid attributes are omitted. */ }
      }
      if (node.tagName === 'DETAILS') copy.setAttribute('open', '');
      if (node.tagName === 'IMG') copy.setAttribute('loading', 'eager');
      for (const child of node.childNodes) visit(child, copy);
      parent.appendChild(copy);
    };
    for (const child of source.childNodes) visit(child, root);
    return root.innerHTML;
  };
  const info = (node) => {
    const meta = node.querySelector('.nsk-content-meta-info');
    return {
      author: (meta?.querySelector('.author-name, a[href*="/space/"], a[href*="/member"]')?.textContent || '').trim().slice(0, 100),
      time: (meta?.querySelector('time')?.textContent || '').trim().slice(0, 100),
      floor: node.id || '',
    };
  };
  const comments = [];
  const seen = new Set();
  let total = 0;
  for (const item of document.querySelectorAll('.comment-container .content-item, ul.comments > li.content-item')) {
    if (item.id === '0') continue;
    const content = item.querySelector('article.post-content');
    const meta = item.querySelector('.nsk-content-meta-info');
    if (!content || !meta) continue;
    // Badge selectors are deliberately limited to metadata: body text saying "HOT" is not a badge.
    const pinned = !!meta.querySelector('.pined-comment-badge, .pinned-comment-badge, [title="置顶"], use[href="#pin"], use[xlink\\:href="#pin"]');
    const hot = !!meta.querySelector('.hot-badge, [title*="HOT"], [title*="Hot"], [title="热评"]');
    if (!pinned && !hot) continue;
    const key = item.getAttribute('data-comment-id') || item.id || content.textContent;
    if (seen.has(key)) continue;
    seen.add(key);
    total++;
    comments.push({...info(item), pinned, hot, html: clean(content)});
  }
  // Pinned comments take precedence when the configured cap is reached.
  comments.sort((a, b) => Number(b.pinned) - Number(a.pinned));
  return {
    title: (document.querySelector('.post-title')?.textContent || document.title).trim().slice(0, 300),
    ...info(post || body.parentElement),
    html: clean(body), comments: comments.slice(0, maxComments), total,
  };
}
