// md_render_bridge.js — Node CJS glue between session-view and md-serial.
//
// Usage: node md_render_bridge.js <path-to-md-serial.mjs> <path-to-render.mjs>
// Input (stdin):  JSON { source, wikilinkMap, blobShas }
// Output (stdout): HTML comment with blob shas + rendered HTML body
//
// source      — raw Markdown text (may include YAML frontmatter)
// wikilinkMap — { [name]: docId } pre-resolved by Python; absent or empty means
//               no resolver, so all wikilinks render as literal [[name]] text
// blobShas    — { mdSerial, render } blob shas from git, written into an HTML
//               comment so a mis-rendered document can be traced to a version
//
// SECURITY: source is untrusted. It is never interpolated into a command line or
// into any script string — it arrives as a JSON field and stays a JS value
// throughout. The renderer escapes every text node and attribute before emitting
// HTML, which is why this bridge does no escaping of its own.
'use strict';

async function main() {
  const [,, mdSerialPath, renderPath] = process.argv;
  if (!mdSerialPath || !renderPath) {
    process.stderr.write(
      'md_render_bridge: usage: node md_render_bridge.js <md-serial.mjs> <render.mjs>\n'
    );
    process.exit(1);
  }

  const { parse } = await import(mdSerialPath);
  const { render, toHTML } = await import(renderPath);

  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }

  let job;
  try {
    job = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch (e) {
    process.stderr.write('md_render_bridge: failed to parse job JSON: ' + e.message + '\n');
    process.exit(1);
  }

  const { source, wikilinkMap, blobShas } = job;

  if (typeof source !== 'string') {
    process.stderr.write('md_render_bridge: job.source must be a string\n');
    process.exit(1);
  }

  const resolveWikilink = (wikilinkMap && typeof wikilinkMap === 'object')
    ? (name) => (Object.prototype.hasOwnProperty.call(wikilinkMap, name) ? wikilinkMap[name] : null)
    : null;

  const doc = parse(source);
  const tree = render(doc, source, { resolveWikilink });
  const body = toHTML(tree);

  let shasComment = '';
  if (blobShas && typeof blobShas === 'object') {
    shasComment = `<!-- md-serial:${blobShas.mdSerial} render:${blobShas.render} -->\n`;
  }

  process.stdout.write(shasComment + body);
}

main().catch(err => {
  process.stderr.write('md_render_bridge error: ' + err.message + '\n');
  if (err.stack) {
    process.stderr.write(err.stack + '\n');
  }
  process.exit(1);
});
