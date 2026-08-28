import brandLogo from '../../assets/brand-logo.png'
import { useI18n } from '../i18n/useI18n'

type BrandLogoVariant = 'mark' | 'compact' | 'hero'

type BrandLogoProps = {
  variant?: BrandLogoVariant
  className?: string
  alt?: string
}

export function BrandLogo({ variant = 'compact', className = '', alt }: BrandLogoProps) {
  const { t } = useI18n()

  return (
    <img
      src={brandLogo}
      alt={alt ?? t('appTitle')}
      className={['brand-logo', `brand-logo--${variant}`, className].filter(Boolean).join(' ')}
      draggable={false}
    />
  )
}
