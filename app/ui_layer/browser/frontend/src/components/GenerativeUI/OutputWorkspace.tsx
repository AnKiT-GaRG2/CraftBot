import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { ArrowUpRight, GripVertical, Layers3, Maximize2, Minimize2, PanelRightClose, PanelRightOpen } from 'lucide-react'
import type { ChatMessage, GenerativeUIArtifact } from '../../types'
import { usePersistedState } from '../../hooks'
import { UI_STATE } from '../../store/uiState'
import { GenerativeUI } from './GenerativeUI'
import styles from './OutputWorkspace.module.css'

const OutputContext = createContext<((artifact: GenerativeUIArtifact) => void) | null>(null)

/** A small reference in the conversation; generated code only runs in the output pane. */
export function OutputReference({ artifact }: { artifact: GenerativeUIArtifact }) {
  const open = useContext(OutputContext)
  const { t } = useTranslation('chat')
  return <button type="button" className={styles.reference} onClick={() => open?.(artifact)} disabled={!open}>
    <span className={styles.referenceIcon}><Layers3 size={19} /></span>
    <span className={styles.referenceText}><strong>{artifact.title}</strong><span>{t('generativeUI.openOutput')}</span></span>
    <ArrowUpRight size={17} />
  </button>
}

export function OutputWorkspace({ sessionId, messages, children }: { sessionId: string; messages: ChatMessage[]; children: ReactNode }) {
  const { t } = useTranslation('chat')
  const [selection, setSelection] = usePersistedState(UI_STATE.chat.outputSelection(sessionId))
  const [splitRatio, setSplitRatio] = usePersistedState(UI_STATE.chat.outputSplitRatio)
  const [dragRatio, setDragRatio] = useState<number | null>(null)
  const [workspaceWidth, setWorkspaceWidth] = useState(0)
  const [fullscreen, setFullscreen] = useState(false)
  const [closingOutput, setClosingOutput] = useState<{ sessionId: string; artifact: GenerativeUIArtifact } | null>(null)
  const workspace = useRef<HTMLDivElement>(null)
  const layoutAnimation = useRef<Animation | null>(null)
  const previousColumns = useRef<string | null>(null)
  const dragging = useRef(false)
  const closeButton = useRef<HTMLButtonElement>(null)
  const showButton = useRef<HTMLButtonElement>(null)
  const previousNewest = useRef<{ sessionId: string; key?: string }>({ sessionId })
  const outputs = useMemo(() => {
    const revisions = new Map<string, GenerativeUIArtifact>()
    for (const message of messages) {
      if (message.style === 'agent' && message.uiArtifact) {
        const artifact = message.uiArtifact
        revisions.set(`${artifact.id}:${artifact.revision}`, artifact)
      }
    }
    return Array.from(revisions.values())
  }, [messages])
  const newest = outputs[outputs.length - 1]
  const newestKey = newest ? `${newest.id}:${newest.revision}` : undefined
  const exiting = closingOutput?.sessionId === sessionId ? closingOutput.artifact : null
  const selected = exiting ?? outputs.find(artifact => `${artifact.id}:${artifact.revision}` === selection) ?? newest
  const isOpen = !!selected && (selection !== 'closed' || !!exiting)
  const compact = workspaceWidth > 0 && workspaceWidth < 860
  const captureLayout = useCallback(() => {
    if (workspace.current) previousColumns.current = getComputedStyle(workspace.current).gridTemplateColumns
    layoutAnimation.current?.cancel()
    layoutAnimation.current = null
  }, [])

  // Incoming outputs open automatically. Prepending older history or a language
  // synchronization must not reopen a pane that the user deliberately closed.
  useEffect(() => {
    const previous = previousNewest.current
    if (previous.sessionId === sessionId && newestKey && previous.key && previous.key !== newestKey) {
      captureLayout()
      setClosingOutput(null)
      setSelection(newestKey)
    }
    previousNewest.current = { sessionId, key: newestKey }
  }, [sessionId, newestKey, setSelection, captureLayout])
  useEffect(() => { setFullscreen(false); setClosingOutput(null) }, [sessionId])
  useLayoutEffect(() => () => { layoutAnimation.current?.cancel(); layoutAnimation.current = null; previousColumns.current = null }, [sessionId])
  useLayoutEffect(() => {
    const element = workspace.current
    const previous = previousColumns.current
    previousColumns.current = null
    if (!element || !previous) return
    const next = getComputedStyle(element).gridTemplateColumns
    if (previous === next || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      if (exiting) finishClose()
      return
    }
    // Animate the shared tracks, so chat, divider, pane and toolbar travel
    // together. No screenshot overlays, document recreation or text scaling.
    const animation = element.animate([
      { gridTemplateColumns: previous }, { gridTemplateColumns: next },
    ], { duration: 300, easing: 'cubic-bezier(.22, 1, .36, 1)' })
    layoutAnimation.current = animation
    void animation.finished.then(() => {
      if (layoutAnimation.current !== animation) return
      layoutAnimation.current = null
      if (exiting) finishClose()
    }).catch(() => { /* Reversing direction or switching sessions cancels motion. */ })
  }, [isOpen, fullscreen, exiting])
  useLayoutEffect(() => {
    const element = workspace.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => setWorkspaceWidth(entry.contentRect.width))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  const minWidth = 320
  const maxWidth = Math.max(minWidth, Math.min(640, workspaceWidth - 440 - 9))
  const conversationWidth = Math.max(minWidth, Math.min(maxWidth, workspaceWidth * (dragRatio ?? splitRatio)))
  const resize = (clientX: number) => {
    const bounds = workspace.current?.getBoundingClientRect()
    if (!bounds?.width) return splitRatio
    return Math.max(minWidth, Math.min(maxWidth, clientX - bounds.left)) / bounds.width
  }

  const open = (artifact: GenerativeUIArtifact) => {
    captureLayout()
    setClosingOutput(null)
    setSelection(`${artifact.id}:${artifact.revision}`)
    requestAnimationFrame(() => closeButton.current?.focus({ preventScroll: true }))
  }
  const finishClose = () => { setClosingOutput(null); setFullscreen(false); requestAnimationFrame(() => showButton.current?.focus({ preventScroll: true })) }
  const close = () => {
    if (exiting) return
    captureLayout()
    // Persist intent immediately; only the visible pane waits for the exit.
    // Switching chats during that exit must not discard the close choice.
    if (selected) setClosingOutput({ sessionId, artifact: selected })
    setSelection('closed')
    setFullscreen(false)
  }
  const toggleFullscreen = () => {
    captureLayout()
    setFullscreen(value => !value)
  }
  return <OutputContext.Provider value={open}>
    <div ref={workspace} style={{ '--conversation-width': `${conversationWidth}px` } as CSSProperties} className={`${styles.workspace} ${isOpen ? styles.withOutput : ''} ${fullscreen ? styles.fullscreen : ''} ${exiting ? styles.closing : ''} ${dragRatio !== null ? styles.resizing : ''}`}>
      <div className={styles.conversation} aria-hidden={fullscreen || (compact && isOpen && !exiting)} {...(fullscreen || (compact && isOpen && !exiting) ? { inert: '' } : {})}>
        {outputs.length > 0 && <div className={styles.conversationBar}>
          <span>{t('generativeUI.conversation')}</span>
          <button type="button" ref={showButton} onClick={() => selected && open(selected)} aria-label={t('generativeUI.showOutputs')} aria-expanded={isOpen}><PanelRightOpen size={16} />{t('generativeUI.outputs')}<span className={styles.outputCount}>{new Set(outputs.map(item => item.id)).size}</span></button>
        </div>}
        <div className={styles.chat}>{children}</div>
      </div>
      {isOpen && selected && <>
      <div className={styles.resizeHandle} role="separator" tabIndex={0} aria-label={t('generativeUI.resizePanel')} aria-orientation="vertical" aria-valuemin={Math.round(minWidth / (workspaceWidth || 1) * 100)} aria-valuemax={Math.round(maxWidth / (workspaceWidth || 1) * 100)} aria-valuenow={Math.round(conversationWidth / (workspaceWidth || 1) * 100)} onDoubleClick={() => setSplitRatio(0.42)} onKeyDown={event => {
        let width = conversationWidth
        if (event.key === 'ArrowLeft') width -= 32
        else if (event.key === 'ArrowRight') width += 32
        else if (event.key === 'Home') width = minWidth
        else if (event.key === 'End') width = maxWidth
        else return
        event.preventDefault()
        if (workspaceWidth) setSplitRatio(Math.max(minWidth, Math.min(maxWidth, width)) / workspaceWidth)
      }} onPointerDown={event => {
        if (event.button !== 0) return
        layoutAnimation.current?.cancel()
        layoutAnimation.current = null
        event.preventDefault()
        event.currentTarget.focus()
        event.currentTarget.setPointerCapture(event.pointerId)
        dragging.current = true
        setDragRatio(resize(event.clientX))
      }} onPointerMove={event => {
        if (dragging.current) setDragRatio(resize(event.clientX))
      }} onPointerUp={event => {
        if (!dragging.current) return
        setSplitRatio(resize(event.clientX))
        dragging.current = false
        setDragRatio(null)
        event.currentTarget.releasePointerCapture(event.pointerId)
      }} onLostPointerCapture={() => { dragging.current = false; setDragRatio(null) }}><GripVertical size={14} /></div>
      <section className={styles.output} aria-label={t('generativeUI.outputPanel')} aria-hidden={!!exiting} {...(exiting ? { inert: '' } : {})} onKeyDown={event => {
        if (event.key === 'Escape' && fullscreen) { event.preventDefault(); toggleFullscreen() }
      }}>
        <GenerativeUI key={`${sessionId}:${selected.id}`} artifact={selected} sessionId={sessionId} toolbarTitle={
          <div className={styles.title}><Layers3 size={17} /><h2 title={selected.title}>{selected.title}</h2></div>
        } toolbarActions={
          <div className={styles.headerActions}>
            {outputs.length > 1 && <select aria-label={t('generativeUI.selectOutput')} value={`${selected.id}:${selected.revision}`} onChange={event => setSelection(event.target.value)}>
              {outputs.map(artifact => <option key={`${artifact.id}:${artifact.revision}`} value={`${artifact.id}:${artifact.revision}`}>{outputs.every(item => item.id === selected.id) ? t('generativeUI.version', { revision: artifact.revision }) : `${artifact.title} · v${artifact.revision}`}</option>)}
            </select>}
            <button type="button" aria-label={t(fullscreen ? 'generativeUI.exitFullscreen' : 'generativeUI.fullscreen')} title={t(fullscreen ? 'generativeUI.exitFullscreen' : 'generativeUI.fullscreen')} onClick={toggleFullscreen}>{fullscreen ? <Minimize2 size={17} /> : <Maximize2 size={17} />}</button>
            <button type="button" ref={closeButton} aria-label={t('generativeUI.closeOutput')} title={t('generativeUI.closeOutput')} onClick={close}><PanelRightClose size={18} /></button>
          </div>
        } />
      </section></>}
    </div>
  </OutputContext.Provider>
}
