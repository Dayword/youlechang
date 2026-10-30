// EXPORTS: IUser, ICurrentUser, MOCK_USERS

export interface IUser {
  id: string;
  username: string;
  email: string;
  password: string;
  createdAt: string;
}

export interface ICurrentUser {
  id: string;
  username: string;
  email: string;
  createdAt: string;
}

export const MOCK_USERS: IUser[] = [
  {
    id: '1',
    username: '小乐乐',
    email: 'xiaole@funpark.com',
    password: '123456',
    createdAt: '2024-01-15T10:30:00.000Z',
  },
  {
    id: '2',
    username: '糖果糖',
    email: 'tangguo@funpark.com',
    password: 'abc123',
    createdAt: '2024-02-20T14:20:00.000Z',
  },
];
