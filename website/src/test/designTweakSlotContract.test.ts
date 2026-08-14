import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { readSlotTranscript, setChatSlotProject } from '../apps/design-tweak/api'

/**
 * The chat-slot API has two responses that both carry a `messages` field, and
 * they mean different things:
 *
 *   POST /api/chat/slots        -> serialize_slot(): `messages` is a COUNT, no queue
 *   GET  /api/chat/slots/{key}  -> prepared message ENTRIES + pending queue
 *
 * Confusing them is silent in both directions: reading the count as a list finds
 * nothing (so every sealed batch looks undelivered and the duplicate resend comes
 * back), and reading it with `.length` yields `undefined` (so an "is the slot
 * empty" guard is always true and re-seeds the session on every mount). Both
 * happened, so both are pinned here.
 */
describe('design-tweak chat-slot response contract', () => {
  const root = process.cwd()
  const api = readFileSync(join(root, 'src/apps/design-tweak/api.ts'), 'utf-8')
  const types = readFileSync(join(root, 'src/apps/design-tweak/types.ts'), 'utf-8')
  const page = readFileSync(join(root, 'src/apps/design-tweak/DesignTweakPage.tsx'), 'utf-8')

  it('types the adopt response `messages` as a number, not a list', () => {
    const block = types.slice(
      types.indexOf('export interface ChatSlotResponse'),
      types.indexOf('}', types.indexOf('export interface ChatSlotResponse')),
    )
    expect(block).toContain('messages?: number')
    expect(block).not.toContain('unknown[]')
  })

  it('reads the transcript from the slot-DETAIL endpoint', () => {
    // The adopt POST cannot answer this question; it has no queue and no entries.
    expect(api).toContain('slotDetailUrl')
    const fn = api.slice(
      api.indexOf('export async function readSlotTranscript'),
      api.indexOf('\n}', api.indexOf('export async function readSlotTranscript')),
    )
    expect(fn).toContain('slotDetailUrl(key)')
    // And it must refuse a response whose messages are not actually a list,
    // rather than treating the count as an empty transcript.
    expect(fn).toContain('Array.isArray(detail.messages)')
  })

  it('gates the session seed on a zero COUNT, never on .length', () => {
    const fn = page.slice(
      page.indexOf('const ensureSlot'),
      page.indexOf('}, [])', page.indexOf('const ensureSlot')),
    )
    expect(fn).toContain('slot?.messages === 0')
    // Strip comments first: the prose above the guard names the old expression
    // to explain the bug, and matching that would be a false positive.
    const code = fn.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
    expect(code).not.toContain('messages?.length')
  })

  it('shows a send control only for a proven-missing request', () => {
    // `needsDeliveryRetry` alone is "unconfirmed", which includes a batch whose
    // ack was merely lost — offering a send there duplicates every edit.
    expect(page).toContain('sendMissing && comments.length > 0')
    expect(page).not.toContain('needsDeliveryRetry(req) && comments.length > 0')
  })

  it('asks the slot dispatch used, addressed by slot key not raw path', () => {
    const fn = page.slice(
      page.indexOf('const verifyDelivery'),
      page.indexOf('}, [projects, previewId, refresh])', page.indexOf('const verifyDelivery')),
    )
    // Adopt is idempotent BY NAME: a raw path adopts a different, empty slot, so
    // every request reads as undelivered and a junk session is created.
    expect(fn).toContain('readSlotTranscript(\n        slotKeyFor(root),')
    const code = fn.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
    expect(code).not.toMatch(/readSlotTranscript\(\s*path\b/)
  })

  it('verifies each request against ITS OWN project root', () => {
    const fn = page.slice(
      page.indexOf('const verifyDelivery'),
      page.indexOf('}, [projects, previewId, refresh])', page.indexOf('const verifyDelivery')),
    )
    // A request exists only in the slot its dispatch used. Resolving ONE path and
    // checking every pending request against it reads other projects' requests as
    // missing and offers a duplicate send for delivered work.
    expect(fn).toContain('byRoot')
    const code = fn.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
    expect(code).not.toMatch(/pending\.find\(\(r\) => r\.projectRoot\)/)
  })

  it('readSlotTranscript refuses a filesystem path as the slot key', () => {
    const fn = api.slice(
      api.indexOf('export async function readSlotTranscript'),
      api.indexOf('\n}', api.indexOf('export async function readSlotTranscript')),
    )
    expect(fn).toContain("slotKey.includes('/')")
    expect(fn).toContain("slotKey.includes('\\\\')")
  })
  it('binds the slot to the previewed project before seeding it', () => {
    const fn = page.slice(
      page.indexOf('const ensureSlot'),
      page.indexOf('}, [])', page.indexOf('const ensureSlot')),
    )
    // `POST /api/chat/slots` takes no project, so without this the slot keeps the
    // dashboard's default: the runner's file search, `@`-mentions and
    // `<project>/.kiro/steering` all resolve against the wrong directory. The
    // seed names absolute paths, so the symptom is silent — steering simply
    // never loads — which is why it is pinned rather than left to review.
    expect(fn).toContain('setChatSlotProject(key, path)')
    // Ordering is load-bearing: the seeded turn must already be scoped.
    expect(fn.indexOf('setChatSlotProject')).toBeLessThan(fn.indexOf('SESSION_SEED'))
    // A failed bind is degraded, not fatal, and must not swallow the seed with it.
    expect(fn).toMatch(/try \{\s*await setChatSlotProject/)
  })
  it('retires the retry control only on a CONFIRMED dispatch', () => {
    const resend = page.slice(
      page.indexOf('const resendRequest'),
      page.indexOf('}, [refresh, deliverSealed])', page.indexOf('const resendRequest')),
    )
    // `deliverSealed` reports its own failures rather than throwing, so a bare
    // `await` tells the caller nothing: clearing on "did not throw" would hide
    // the button after a failed send, and clearing never at all leaves it live
    // so a second click dispatches the same edits again.
    expect(resend).toContain('const dispatched = await deliverSealed(req, req)')
    expect(resend).toContain('if (dispatched)')
    expect(resend).toContain('setMissingIds')
    const code = resend.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
    expect(code).not.toMatch(/^\s*await deliverSealed\(req, req\)\s*$/m)
  })

  it('deliverSealed reports success or failure on every path', () => {
    const fn = page.slice(
      page.indexOf('const deliverSealed'),
      page.indexOf('}, [projects, previewId, ensureSlot])', page.indexOf('const deliverSealed')),
    )
    // Three exits: no session, dispatch threw, dispatch confirmed. A path that
    // falls out returning `undefined` is indistinguishable from failure at the
    // call site, which is what made the retry button's state unreliable.
    expect(fn).toContain('return false')
    expect(fn).toContain('return true')
    expect((fn.match(/return false/g) || []).length).toBeGreaterThanOrEqual(2)
  })
})

describe('setChatSlotProject', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('posts the project to the slot-scoped endpoint', async () => {
    const fetchMock = vi.fn(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await setChatSlotProject('design-tweak-abc', '/Users/me/proj')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat/slots/design-tweak-abc/project')
    expect(init.method).toBe('POST')
    // The backend reads `project` and rejects a non-string with 400.
    expect(JSON.parse(String(init.body))).toEqual({ project: '/Users/me/proj' })
  })

  it('encodes a slot key so it cannot forge a path segment', async () => {
    const fetchMock = vi.fn(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await setChatSlotProject('a/b', '/tmp')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/chat/slots/a%2Fb/project')
  })
})

describe('readSlotTranscript behavior', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('makes NO request for a raw path, so no junk slot is adopted', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    expect(await readSlotTranscript('/Users/me/proj', 'Design Tweak')).toBeNull()
    expect(await readSlotTranscript('C:\\Users\\me\\proj', 'Design Tweak')).toBeNull()
    // The point is the absence of the call: adopting is a side effect that would
    // create a session named after a filesystem path.
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('returns null rather than treating a message COUNT as a transcript', async () => {
    // Shape of the adopt response, which is what the code used to read.
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ key: 'dt-abc', messages: 7 }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    // Detail returns the same count-shaped body -> not a list -> unknown, not empty.
    expect(await readSlotTranscript('dt-abc', 'Design Tweak')).toBeNull()
  })

  it('returns the transcript when the detail read yields real entries', async () => {
    const fetchMock = vi.fn(async (url: string | URL | Request) => {
      const u = String(url)
      const body = u.includes('/api/chat/slots/')
        ? { key: 'dt-abc', messages: [{ role: 'user', content: 'req-1' }], queue: [] }
        : { key: 'dt-abc', messages: 3 }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const t = await readSlotTranscript('dt-abc', 'Design Tweak')
    expect(Array.isArray(t?.messages)).toBe(true)
    expect(t?.messages).toHaveLength(1)
  })
})
