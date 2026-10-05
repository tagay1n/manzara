import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHarness} from './support/page-harness.mjs';
const source = readFileSync(new URL('../../static/library-publishers.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../../static/library-publishers.html', import.meta.url), 'utf8');
const ids = [...html.matchAll(/id="([^"]+)"/g)].map(match => match[1]);
const click = (h, id, selector, dataset) => h.elements.get(id).dispatch('click', {target:{closest: value => value === selector ? {dataset} : null}});
function setup({failEdit = false, failDraft = false, applyGate = null, editGate = null, count = 55} = {}) {
  const items = Array.from({length:count}, (_, i) => ({key:`canonical:${i+1}`, canonical_id:i+1, display_name:`Publisher ${String(i+1).padStart(3,'0')}`, aliases:['Variant'], document_count:i+1, is_new:false}));
  const proposals = [1,2].map(id => ({proposal_id:id, status:'pending', members:items.slice(0,2), proposed_aliases:['Variant'], proposal:{kind:'cluster', member_ids:items.slice(0,2).map(row=>row.key), proposed_name:`Group ${id}`, rationale:'Research rationale', confidence:'possible', citations:[]}}));
  let review = {revision:0, draft:{renames:[],keeps:[],merges:[]}, proposals};
  const h = createHarness({source, ids, apiResolver(path, options={}) {
    if (path === '/api/library/publishers') return {items, new_count:0, publisher_count:count};
    if (path === '/api/library/publishers/merge-suggestions') return review;
    if (path.endsWith('/draft')) {if (failDraft) throw new Error('Draft save failed'); review = {...review, revision:review.revision+1, draft:JSON.parse(options.body).changes}; return review;}
    if (path.endsWith('/draft/discard')) { review = {...review, draft:{renames:[],keeps:[],merges:[]}, proposals:review.proposals.map(p=>({...p,review_edit:null}))}; return review; }
    if (path.endsWith('/change-set/apply')) return applyGate || {ok:true};
    if (path.endsWith('/review')) {
      const payload = JSON.parse(options.body), id = Number(path.split('/').at(-2));
      if (payload.action === 'edit' && failEdit) throw new Error('Save failed');
      review = {...review, proposals:review.proposals.map(p => p.proposal_id !== id ? p : {...p, review_edit:{display_name:payload.display_name,member_ids:payload.member_ids}, status:payload.action==='skip'?'skipped':p.status})};
      return editGate && payload.action === 'edit' ? editGate.then(() => review) : review;
    }
    if (path.includes('/documents?')) return {items:[{md5:'a'.repeat(32), label:'Book.pdf'}],page:1,has_more:false};
    throw new Error(`Unexpected ${path}`);
  }});
  return h;
}
test('directory starts compact with a hidden changes bar and accessible tabs', async () => {
  const h=setup(); await h.flush();
  assert.equal(h.elements.get('tab-panel-directory').hidden,false);
  assert.equal(h.elements.get('tab-panel-review').hidden,true);
  assert.equal(h.elements.get('publisher-changes').hidden,true);
  assert.equal(h.elements.get('publisher-review-layout').classList.contains('has-detail'),false);
  assert.doesNotMatch(h.elements.get('publisher-table-body').innerHTML,/Show documents|Show .*aliases/);
  assert.equal((h.elements.get('publisher-table-body').innerHTML.match(/publisher-row-select/g)||[]).length,50);
  assert.equal(h.elements.get('tab-btn-directory').getAttribute('aria-selected'),'true');
  h.elements.get('tab-btn-review').dispatch('click'); await h.flush();
  assert.equal(h.elements.get('tab-panel-directory').hidden,true);
  assert.equal(h.elements.get('tab-panel-review').hidden,false);
});
test('selection spans pages and search while select-all affects only the page', async () => {
  const h=setup(); await h.flush();
  h.elements.get('publisher-select-page').dispatch('change',{target:{checked:true}});
  h.elements.get('publisher-page-next').dispatch('click');
  assert.equal(h.elements.get('publisher-selected-count').textContent,'50 selected');
  h.elements.get('publisher-filter-input').value='055';
  h.elements.get('publisher-filter-input').dispatch('input');
  h.elements.get('publisher-merge').dispatch('click');
  assert.equal((h.elements.get('publisher-merge-selected').innerHTML.match(/publisher-merge-member/g)||[]).length,50);
});
test('publisher opens a side panel with aliases and document pagination', async () => {
  const h=setup(); await h.flush();
  click(h,'publisher-table-body','.publisher-name',{key:'canonical%3A1'}); await h.flush();
  assert.equal(h.elements.get('publisher-detail').hidden,false);
  assert.match(h.elements.get('publisher-detail-body').innerHTML,/Variant|publisher-detail-name/);
  click(h,'publisher-detail-body','.publisher-documents-toggle',{key:'canonical%3A1'}); await h.flush();
  assert.match(h.elements.get('publisher-detail-body').innerHTML,/Book.pdf/);
});
test('review edits are saved before selecting another stable proposal', async () => {
  const h=setup(); await h.flush();
  const input=h.document.getElementById('publisher-suggestion-name'); input.value='Edited';
  h.elements.get('publisher-suggestions-body').dispatch('input',{target:input});
  click(h,'publisher-suggestions-list','.publisher-proposal-open',{proposalId:'2'}); await h.flush();
  const saved=h.apiCalls.find(call=>call.path.endsWith('/1/review'));
  assert.equal(JSON.parse(saved.options.body).display_name,'Edited');
  assert.match(h.elements.get('publisher-suggestions-body').innerHTML,/value="Group 2"/);
  assert.equal(h.elements.get('publisher-changes').hidden,false);
  assert.equal(h.elements.get('publisher-apply').disabled,true);
});
test('a failed edit blocks navigation and retains the edited name', async () => {
  const h=setup({failEdit:true}); await h.flush();
  const input=h.document.getElementById('publisher-suggestion-name'); input.value='Unsaved';
  h.elements.get('publisher-suggestions-body').dispatch('input',{target:input});
  h.elements.get('tab-btn-directory').dispatch('click'); await h.flush();
  click(h,'publisher-suggestions-list','.publisher-proposal-open',{proposalId:'2'}); await h.flush();
  assert.match(h.elements.get('publisher-suggestions-body').innerHTML,/value="Unsaved"/);
  assert.doesNotMatch(h.elements.get('publisher-suggestions-body').innerHTML,/value="Group 2"/);
});
test('skip advances and skipped proposals can be revisited', async () => {
  const h=setup(); await h.flush();
  click(h,'publisher-suggestions-body','.publisher-suggestion-action',{action:'skip'}); await h.flush();
  assert.match(h.elements.get('publisher-suggestions-body').innerHTML,/value="Group 2"/);
  const filter=h.elements.get('publisher-review-status'); filter.value='skipped';
  filter.dispatch('change',{target:filter}); await h.flush();
  assert.match(h.elements.get('publisher-suggestions-body').innerHTML,/value="Group 1"/);
});

test('a rename is staged before leaving publisher details and discard clears it', async () => {
  const h=setup(); await h.flush();
  click(h,'publisher-table-body','.publisher-name',{key:'canonical%3A1'}); await h.flush();
  const input=h.document.getElementById('publisher-detail-name'); input.value='Renamed';
  h.elements.get('publisher-detail-body').dispatch('input',{target:input});
  h.elements.get('tab-btn-review').dispatch('click'); await h.flush();
  const saved=h.apiCalls.find(call=>call.path.endsWith('/draft'));
  assert.deepEqual(JSON.parse(saved.options.body).changes.renames,[{canonical_id:1,display_name:'Renamed'}]);
  assert.equal(h.elements.get('publisher-changes').hidden,false);
  assert.equal(h.elements.get('publisher-apply').disabled,false);
  h.elements.get('publisher-discard').dispatch('click'); await h.flush();
  assert.equal(h.elements.get('publisher-changes').hidden,true);
  assert.doesNotMatch(h.elements.get('publisher-table-body').innerHTML,/Renamed/);
});

test('keyboard tab navigation selects and exposes the matching panel', async () => {
  const h=setup(); await h.flush();
  h.elements.get('tab-btn-directory').dispatch('keydown',{key:'ArrowRight',preventDefault(){}}); await h.flush();
  assert.equal(h.elements.get('tab-btn-review').getAttribute('aria-selected'),'true');
  assert.equal(h.elements.get('tab-panel-review').hidden,false);
  h.elements.get('tab-btn-review').dispatch('keydown',{key:'Home',preventDefault(){}}); await h.flush();
  assert.equal(h.elements.get('tab-panel-directory').hidden,false);
});

test('Back returns from review details without losing the proposal selection', async () => {
  const h=setup(); await h.flush();
  click(h,'publisher-suggestions-list','.publisher-proposal-open',{proposalId:'2'}); await h.flush();
  h.elements.get('publisher-review-back').dispatch('click'); await h.flush();
  assert.equal(h.elements.get('publisher-review-layout').classList.contains('has-detail'),false);
  assert.match(h.elements.get('publisher-suggestions-body').innerHTML,/value="Group 2"/);
});


test('repeated Apply clicks send one request while preparation is asynchronous', async () => {
  let finish;
  const applyGate=new Promise(resolve=>{finish=resolve;});
  const h=setup({applyGate}); await h.flush();
  click(h,'publisher-table-body','.publisher-name',{key:'canonical%3A1'}); await h.flush();
  const input=h.document.getElementById('publisher-detail-name'); input.value='Renamed';
  h.elements.get('publisher-detail-body').dispatch('input',{target:input});
  h.elements.get('publisher-detail-back').dispatch('click'); await h.flush();
  h.elements.get('publisher-apply').dispatch('click');
  h.elements.get('publisher-apply').dispatch('click'); await h.flush();
  const requests=h.apiCalls.filter(call=>call.path.endsWith('/change-set/apply'));
  finish({ok:true}); await h.flush();
  assert.equal(requests.length,1);
});

test('a failed draft save retains the local rename and blocks navigation', async () => {
  const h=setup({failDraft:true}); await h.flush();
  click(h,'publisher-table-body','.publisher-name',{key:'canonical%3A1'}); await h.flush();
  const input=h.document.getElementById('publisher-detail-name'); input.value='Retained rename';
  h.elements.get('publisher-detail-body').dispatch('input',{target:input});
  h.elements.get('publisher-detail-back').dispatch('click'); await h.flush();
  assert.equal(h.elements.get('publisher-detail').hidden,false);
  assert.match(h.elements.get('publisher-detail-body').innerHTML,/value="Retained rename"/);
  assert.equal(h.elements.get('publisher-changes').hidden,false);
});

test('navigation waits for an in-flight save and saves subsequent typing', async () => {
  let finish;
  const editGate=new Promise(resolve=>{finish=resolve;});
  const h=setup({editGate}); await h.flush();
  const input=h.document.getElementById('publisher-suggestion-name'); input.value='First edit';
  h.elements.get('publisher-suggestions-body').dispatch('input',{target:input});
  const saving=h.timer.runAllTimeouts(); await h.flush();
  input.value='Later edit'; h.elements.get('publisher-suggestions-body').dispatch('input',{target:input});
  click(h,'publisher-suggestions-list','.publisher-proposal-open',{proposalId:'2'}); await h.flush();
  assert.match(h.elements.get('publisher-suggestions-body').innerHTML,/value="Group 1"/);
  finish(); await saving; await h.flush();
  const requests=h.apiCalls.filter(call=>call.path.endsWith('/1/review')).map(call=>JSON.parse(call.options.body));
  assert.deepEqual(requests.map(request=>request.display_name),['First edit','Later edit']);
  assert.match(h.elements.get('publisher-suggestions-body').innerHTML,/value="Group 2"/);
});
