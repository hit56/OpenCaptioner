import { useI18n } from '../../shared/i18n/useI18n'
import { formatSpkDuration } from './formatSpkDuration'
import { getSpeakerAvatar } from './speakerAvatar'
import type { SpeakerStatItem } from './speakerStatsUtils'

interface SpeakerStatsBarProps {
  speakers: SpeakerStatItem[]
  activeSpeakerId: string | null
  detectedLang?: string
  detectedLangName?: string
  onToggleSpeaker: (speakerId: string) => void
  onDeleteSpeaker: (speakerId: string) => void
}

export function SpeakerStatsBar({
  speakers,
  activeSpeakerId,
  detectedLang,
  detectedLangName,
  onToggleSpeaker,
  onDeleteSpeaker,
}: SpeakerStatsBarProps) {
  const { t } = useI18n()
  if (!speakers.length) return null

  const showLang = detectedLang && detectedLangName

  return (
    <div id="speaker-stats-bar">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, width: '100%' }}>
        <div id="spk-avatar-container">
          {speakers.map((speaker) => {
            const spkId = String(speaker.id)
            const isActive = activeSpeakerId === spkId
            return (
              <div
                key={spkId}
                className={`spk-summary-item${isActive ? ' active' : ''}`}
                data-speaker-id={spkId}
                data-click-action="upload_speaker_toggle"
                data-click-label="筛选说话人"
                data-click-tab="upload"
                role="button"
                tabIndex={0}
                onClick={() => onToggleSpeaker(spkId)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    onToggleSpeaker(spkId)
                  }
                }}
              >
                <div
                  className="spk-delete-all-btn"
                  role="button"
                  tabIndex={0}
                  title={t('deleteSpeakerAll')}
                  data-click-action="upload_speaker_delete"
                  data-click-label={t('deleteSpeakerAll')}
                  data-click-tab="upload"
                  onClick={(event) => {
                    event.stopPropagation()
                    onDeleteSpeaker(spkId)
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      event.stopPropagation()
                      onDeleteSpeaker(spkId)
                    }
                  }}
                >
                  ×
                </div>
                <img src={getSpeakerAvatar(spkId, speaker.gender)} className="spk-summary-img" alt="" />
                <div className="spk-info-col">
                  <span className="spk-summary-id">Spk {spkId}</span>
                  <span className="spk-summary-time">{formatSpkDuration(speaker.duration)}</span>
                </div>
              </div>
            )
          })}
        </div>
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'flex-start',
            justifyContent: 'center',
            flexShrink: 0,
            minWidth: 'max-content',
            lineHeight: 1.45,
          }}
        >
          <span className="overview-meta-text">
            <span id="lbl-spk-count">{t('speakerCountLabel')}</span>
            <span id="spk-total-count">{speakers.length}</span>
            <span id="lbl-persons">{t('persons')}</span>
          </span>
          {showLang ? (
            <span id="lang-detect-label" className="overview-meta-text" style={{ marginTop: 2 }}>
              {t('langDetect')}
              {detectedLangName || detectedLang}
            </span>
          ) : null}
        </div>
      </div>
    </div>
  )
}
