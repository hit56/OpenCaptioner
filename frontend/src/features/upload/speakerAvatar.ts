const speakerGenderCache: Record<string, 'boy' | 'girl'> = {}

export function getSpeakerAvatar(speakerId?: string | null, gender?: string | null): string {
  let id = speakerId === undefined || speakerId === null ? 'unknown' : String(speakerId)
  if (gender) {
    speakerGenderCache[id] = gender === 'male' ? 'boy' : 'girl'
  }
  let genderType = speakerGenderCache[id]
  if (!genderType) {
    const numId = parseInt(id, 10)
    genderType = numId % 2 === 0 ? 'boy' : 'girl'
  }
  const numId = parseInt(id, 10)
  const imgIndex = (Number.isNaN(numId) ? 1 : numId % 12) + 1
  return `/avatars/${genderType}/${genderType}${imgIndex}.jpg`
}
