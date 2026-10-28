import {useId, useState} from 'react'
import {Tabs, TabsContent, TabsList, TabsTrigger} from './components/ui/tabs'
import './ExplanationView.css'

export type BoundExplanation = {
  call_id: string
  title: string
  purpose: string
  subject: string
  panels: {
    id: string
    title: string
    explanation: string
    provenance: 'source' | 'illustrative'
    svg_url: string
  }[]
}

type Panel = BoundExplanation['panels'][number]

function boundSvgUrl(panel: Panel): string | null {
  // Only the bound asset route is eligible; never turn model text into markup.
  const match = /^\/api\/bridge\/assets\/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\/goal_explanation_([1-9][0-9]*)_([a-zA-Z0-9_-]{1,31})\.svg$/.exec(panel.svg_url)
  return match && match[0] === panel.svg_url && match[3] === panel.id ? panel.svg_url : null
}

function ExplanationPanel({panel, subject}: {panel: Panel; subject: string}) {
  const headingId = useId()
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  const url = boundSvgUrl(panel)
  const label = panel.provenance === 'source' ? '整理示意' : '解释性示例'

  return <div className="vaa-explanation__stage" aria-labelledby={headingId}>
    <div className="vaa-explanation__canvas">
      {url && failedUrl !== url
        ? <img
          src={url}
          alt={`${subject}：${panel.title}（${label}）`}
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => setFailedUrl(url)}
        />
        : <p className="vaa-explanation__unavailable" role="status">这张示意图暂时无法显示，仍可阅读说明文字。</p>}
    </div>
    <div className="vaa-explanation__detail">
      <span className="vaa-explanation__provenance">{label}</span>
      <h4 id={headingId} className="vaa-explanation__panel-title">{panel.title}</h4>
      <p className="vaa-explanation__text">{panel.explanation}</p>
    </div>
  </div>
}

function ExplanationPanels({value}: {value: BoundExplanation}) {
  const headingId = useId()
  const [selected, setSelected] = useState(value.panels[0]?.id ?? '')
  const first = value.panels[0]

  return <section className="vaa-explanation" aria-labelledby={headingId}>
    <header className="vaa-explanation__header">
      <h3 id={headingId} className="vaa-explanation__title">{value.title}</h3>
    </header>
    {value.panels.length > 1
      ? <Tabs value={selected} onValueChange={setSelected} activationMode="automatic" className="vaa-explanation__tabs">
        <TabsList className="vaa-explanation__choices" aria-label={`${value.title}：选择说明内容`}>
          {value.panels.map(panel => <TabsTrigger key={panel.id} value={panel.id} className="vaa-explanation__choice">
            {panel.title}
          </TabsTrigger>)}
        </TabsList>
        {value.panels.map(panel => <TabsContent key={panel.id} value={panel.id} className="vaa-explanation__panel">
          <ExplanationPanel panel={panel} subject={value.subject}/>
        </TabsContent>)}
      </Tabs>
      : first
        ? <ExplanationPanel panel={first} subject={value.subject}/>
        : <p className="vaa-explanation__text">暂时没有可显示的示意内容。</p>}
  </section>
}

export default function ExplanationView({value}: {value: BoundExplanation}) {
  // A new bound artifact starts at its first panel, even if its call id is reused.
  const identity = JSON.stringify([value.call_id, value.subject, value.panels.map(panel => [panel.id, panel.svg_url])])
  return <ExplanationPanels key={identity} value={value}/>
}
