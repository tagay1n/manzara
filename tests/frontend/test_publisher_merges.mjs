import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHarness} from './support/page-harness.mjs';
const source = readFileSync(new URL('../../static/library-publishers.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../../static/library-publishers.html', import.meta.url), 'utf8');
const ids = [...html.matchAll(/id="([^"]+)"/g)].map(match => match[1]);

function setup(conflicts = false, singleton = false) {
  let savedEdit = false;
  return createHarness({source, ids, apiResolver(path) {
    if (path === '/api/library/publishers') return {items: [], new_count: 0, publisher_count: 0};
    if (path.endsWith('/review')) savedEdit = true;
    if (path === '/api/library/publishers/merge-suggestions' || path.endsWith('/review')) return {coverage:{inventory_entries:3,covered_entries:3,cluster_count:1,singleton_entries:1,unresolved_entries:0}, revision: 1, draft: {renames: [], keeps: [], merges: []}, proposals: [{proposal_id: 1, status: 'pending', conflict_member_ids: conflicts && !savedEdit ? ['raw:One'] : [], conflicting_proposal_ids: conflicts && !savedEdit ? [2] : [], members: [{key: 'raw:One', display_name: '<script>One</script>', aliases: ['<img>']}], proposed_aliases:['Second spelling','<img>alias'], proposal: {kind:'cluster', member_ids: ['raw:One', 'raw:Two'], proposed_name: '<script>One</script>" onfocus="alert(1)', rationale: '<img>same', confidence: 'uncertain', uncertainty: 'Needs evidence', citations: [{url: 'javascript:alert(1)', supports: 'unsafe'}, {url: 'https://example.org', supports: '<img>history'}]}}, ...(conflicts ? [{proposal_id: 2, status:'pending', members:[], proposal:{kind:'cluster',member_ids:['raw:One','raw:Three'], proposed_name:'Three', rationale:'Second group', confidence:'possible', citations:[]}}] : []), ...(singleton ? [{proposal_id:3,status:'pending',proposed_aliases:[],members:[{key:'raw:Solo',display_name:'Solo',aliases:[],is_new:true}],proposal:{kind:'singleton',member_ids:['raw:Solo'],proposed_name:'Solo',rationale:'Standalone candidate',confidence:'possible',uncertainty:'',citations:[]}}] : [])]};
    throw new Error(`Unexpected ${path}`);
  }});
}

test('publisher suggestions escape model content and reject unsafe links', async () => {
  const harness = setup(); await harness.flush();
  const body = harness.elements.get('publisher-suggestions-body').innerHTML;
  assert.match(body, /Uncertain/);
  assert.match(body, /&quot; onfocus=&quot;/);
  assert.doesNotMatch(body, /value="[^"<>]*" onfocus=/);
  assert.match(body, /&lt;script&gt;One/);
  assert.match(body, /&lt;img&gt;same/);
  assert.doesNotMatch(body, /javascript:/);
  assert.match(body, /https:\/\/example.org/);
  assert.match(body, /Stage merge/);
  assert.match(body, /Keep separate/);
  assert.match(body, /aria-label="Remove/);
});

test('review controls use native buttons and labeled editable names', () => {
  assert.match(source, /aria-label="Canonical publisher name"/);
  assert.match(html, /id="publisher-suggestions-list"/);
  assert.match(source, /<button class="publisher-proposal-open/);
});


test('conflicts identify shared names and open the related proposal for review', async () => {
  const harness = setup(true); await harness.flush();
  const body = harness.elements.get('publisher-suggestions-body');
  assert.match(body.innerHTML, /<strong>Conflict<\/strong>/);
  assert.match(body.innerHTML, /&lt;script&gt;One/);
  assert.match(body.innerHTML, /data-proposal-id="2"[^>]*>Three/);
  body.dispatch('click', {target:{closest(selector) {
    return selector === '.publisher-conflict-open' ? {dataset:{proposalId:'2'}} : null;
  }}});
  await harness.flush();
  assert.match(body.innerHTML, /Second group/);
});


test('saved member edits refresh conflict markers without reloading the page', async () => {
  const harness = setup(true); await harness.flush();
  const body = harness.elements.get('publisher-suggestions-body');
  assert.match(body.innerHTML, /<strong>Conflict<\/strong>/);
  body.dispatch('click', {target:{closest(selector) {
    return selector === '.publisher-suggestion-remove' ? {dataset:{key:'raw%3AOne'}} : null;
  }}});
  await harness.flush();
  assert.doesNotMatch(body.innerHTML, /<strong>Conflict<\/strong>/);
});


test('cluster review presents a proposed publisher with variants and coverage', async () => {
  const harness=setup(); await harness.flush();
  const body=harness.elements.get('publisher-suggestions-body').innerHTML;
  assert.match(body,/id="publisher-suggestion-name"/);
  assert.doesNotMatch(body,/Proposed publisher/);
  assert.match(body,/Aliases \(2\)/);
  assert.match(body,/Second spelling/);
  assert.match(body,/&lt;img&gt;alias/);
  assert.match(harness.elements.get('publisher-clustering-coverage').textContent,/3 \/ 3/);
});


test('category filters expose singleton publishers separately from clusters', async () => {
  const harness=setup(false,true); await harness.flush();
  const body=harness.elements.get('publisher-suggestions-body');
  assert.doesNotMatch(body.innerHTML,/Standalone candidate/);
  const filter=harness.elements.get('publisher-cluster-filter');
  filter.value='singleton'; filter.dispatch('change',{target:filter});
  await harness.flush();
  assert.match(body.innerHTML,/value="Solo"/);
  assert.match(body.innerHTML,/Standalone candidate/);
  assert.match(body.innerHTML,/data-action="stage"[^>]*>Keep/);
  assert.doesNotMatch(body.innerHTML,/Stage merge/);
});
